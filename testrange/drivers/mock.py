"""MockDriver — an in-memory :class:`HypervisorDriver` for tests and dev.

Implements the full driver contract without touching a real hypervisor:
volumes are real files under a ``pool_root`` (so the cache upload/download
round-trip is exercised), every other resource lives in in-memory dicts, and
the native guest agent is simulated. Every backend call is appended to
``calls`` so tests can assert the orchestrator's call sequence.

It is the canonical substrate the orchestrator and ABC-contract tests run
against while no real driver is shipped. The native agent is QGA-shaped —
unauthenticated, all three ops supported — so all three ``native_guest_*``
accessors are live.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Self

from testrange.communicators.base import ExecResult
from testrange.connect import BackendProfile, register_profile
from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.exceptions import DriverError, GuestAgentError
from testrange.hypervisor import Hypervisor
from testrange.networks.base import ManagedBuildSwitch
from testrange.networks.sidecar import LEASEFILE
from testrange.preflight import (
    PreflightFinding,
    PreflightReport,
    mgmt_unsupported_findings,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.devices.pool.base import StoragePool
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import ManagedEgress, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec

_MOCK_OUI = "02:00:00"  # locally-administered, unicast
_SUFFIXES = {
    "build_disk": ".qcow2",
    "run_disk": ".qcow2",
    "data_disk": ".qcow2",
    "base_image": ".qcow2",
    "build_seed": ".iso",
    "sidecar_disk": ".qcow2",
    "sidecar_config": ".iso",
}


@dataclass(frozen=True)
class MockHypervisor(Hypervisor):
    """Topology-only scheme marker selecting the in-memory ``mock`` backend (CORE-19).

    Identical in shape to the generic :class:`~testrange.Hypervisor`; its only
    job is to assert *this topology MUST run against the mock backend* so a
    preflight (and a human reader) can catch a mismatched ``--connect`` early.
    The mock-side env knobs (``pool_root`` / ``backing_capacity_gb``) live on
    :class:`MockProfile`; connection is **always** supplied via ``--connect``.
    """


@dataclass
class _Switch:
    backend_name: str
    uplink_network: str | None


# Default serial output of a clean build: the positive token the orchestrator
# treats as the only success signal.
_DEFAULT_BUILD_RESULT = (b"TESTRANGE-RESULT: ok\n",)


class MockDriver(HypervisorDriver):
    """In-memory driver. Tracks every call in ``calls`` for assertions."""

    DRIVER_NAME = "MockDriver"

    def __init__(
        self,
        *,
        uri: str = "mock:///",
        pool_root: Path | None = None,
        backing_capacity_gb: int | None = None,
    ) -> None:
        self.uri = uri
        self.pool_root = pool_root or Path(tempfile.mkdtemp(prefix="testrange-mock-"))
        self.backing_capacity_gb = backing_capacity_gb
        self.connected = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        # Resource ledgers.
        self._switches: dict[str, _Switch] = {}
        self._networks: dict[str, str] = {}  # network backend -> switch backend
        self._pools: set[str] = set()
        self._vms: dict[str, str] = {}  # vm backend -> power state
        self._snapshots: dict[str, list[str]] = {}
        self._volume_sizes: dict[str, int] = {}  # ref -> last size_gb seen

        # Test knobs.
        self.shutoff_after_calls = 1
        self.power_state_calls = 0
        self.fail_create_vm = False
        self.preflight_override: PreflightReport | None = None
        # Raw dnsmasq lease text served off the sidecar over the native agent;
        # empty falls back to the table auto-registered by compose_mac.
        self.sidecar_leases: str = ""
        self._lease_table: dict[str, str] = {}
        # When True, native guest-file reads raise GuestAgentError — simulates a
        # sidecar whose agent never answers, for the readiness-gate test.
        self.guest_agent_unreachable = False
        # Build-result sink knobs. ``build_result_stream`` overrides the serial
        # chunks the next read_build_result_sink replays (inject a ``fail``
        # record, or boot chatter with no record to model a crash). ``None``
        # replays the default ``ok``. ``build_result_wedge`` makes the sink
        # heartbeat forever so the build-timeout watchdog is reachable.
        self.build_result_stream: Sequence[bytes] | None = None
        self.build_result_wedge = False

    # -- construction paths ------------------------------------------------

    @classmethod
    def from_uri(cls, uri: str) -> MockDriver:
        return cls(uri=uri)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        self.connected = True
        self._record("connect")

    def disconnect(self) -> None:
        self.connected = False
        self._record("disconnect")

    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch
    ) -> PreflightReport:
        del cache_manager, build_switch
        self._record("preflight")
        if self.preflight_override is not None:
            return self.preflight_override
        findings: list[PreflightFinding] = list(mgmt_unsupported_findings(plan))
        findings.extend(self._pool_capacity_findings(plan))
        return PreflightReport(findings=tuple(findings))

    def _pool_capacity_findings(self, plan: Plan) -> list[PreflightFinding]:
        """Verify the single backing store holds at least each pool's ``size_gb``."""
        if self.backing_capacity_gb is None:
            return []
        out: list[PreflightFinding] = []
        for pool in plan.hypervisor.pools:
            if pool.size_gb > self.backing_capacity_gb:
                out.append(
                    PreflightFinding(
                        code="pool-capacity",
                        message=(
                            f"pool {pool.name!r} needs {pool.size_gb} GiB but the backing "
                            f"store has only {self.backing_capacity_gb} GiB"
                        ),
                        fix_hint="lower the pool size_gb or point the driver at a larger store",
                    )
                )
        return out

    # -- deterministic naming ----------------------------------------------

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return f"tr_{kind}_{run_id[:8]}_{name}"

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        digest = hashlib.sha256(f"{plan_name}/{vm_name}/{nic_idx}".encode()).digest()
        mac = f"{_MOCK_OUI}:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"
        # Auto-register a deterministic lease so sidecar DHCP discovery
        # succeeds by default; a test wanting a specific IP sets sidecar_leases.
        self._lease_table[mac.lower()] = f"10.0.1.{digest[2] % 254 + 1}"
        return mac

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return VolumeRef(str(self.pool_root / pool_backend_name / vol_name))

    # -- switches & networks (driver owns L2) ------------------------------

    def create_switch(
        self, switch: Switch, backend_name: str, *, managed_egress: ManagedEgress | None = None
    ) -> str | None:
        nat = switch.sidecar is not None and switch.sidecar.nat
        uplink_network: str | None = None
        if managed_egress is not None:
            # Simulate the manufactured + fenced egress segment the sidecar's
            # eth1 rides (a real backend SNATs + fences it; the mock just names it).
            uplink_network = f"{backend_name}__managed_egress"
        elif switch.uplink is not None and nat:
            uplink_network = f"{backend_name}__uplink"
        self._switches[backend_name] = _Switch(backend_name, uplink_network)
        self._record(
            "create_switch",
            backend_name,
            switch.name,
            switch.uplink,
            nat,
            managed_egress is not None,
        )
        return uplink_network

    def destroy_switch(self, backend_name: str) -> None:
        self._record("destroy_switch", backend_name)
        self._switches.pop(backend_name, None)

    def create_network(
        self,
        network: Network,
        switch: Switch,
        backend_name: str,
        *,
        switch_backend_name: str,
    ) -> Any:
        self._networks[backend_name] = switch_backend_name
        self._record("create_network", backend_name, network.name, switch.name, switch_backend_name)
        return f"net:{backend_name}"

    def destroy_network(self, backend_name: str) -> None:
        self._record("destroy_network", backend_name)
        self._networks.pop(backend_name, None)

    # -- pools & volumes ---------------------------------------------------

    def volume_suffix(self, kind: str) -> str:
        return _SUFFIXES[kind]

    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        self._record("create_pool", backend_name, pool.name)
        (self.pool_root / backend_name).mkdir(parents=True, exist_ok=True)
        self._pools.add(backend_name)
        return f"pool:{backend_name}"

    def destroy_pool(self, backend_name: str) -> None:
        self._record("destroy_pool", backend_name)
        self._pools.discard(backend_name)

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        self._record("write_to_pool", str(target_ref), len(data))
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return target_ref

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        self._record("create_blank_volume", str(target_ref), size_gb)
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deterministic sized placeholder: encodes the size so a build->cache
        # ->run round-trip of a data disk's bytes is observable in tests.
        path.write_bytes(f"MOCK-BLANK:{size_gb}G\n".encode())
        self._volume_sizes[str(target_ref)] = size_gb
        return target_ref

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        self._record("resize_volume", str(target_ref), size_gb)
        path = Path(target_ref)
        if not path.exists():
            raise DriverError(f"resize_volume: no volume at {target_ref!r}")
        prior = self._volume_sizes.get(str(target_ref))
        if prior is not None and size_gb < prior:
            raise DriverError(
                f"resize_volume: cannot shrink {target_ref!r} from {prior}G to {size_gb}G"
            )
        # Content is untouched (grow-in-place); only the recorded size moves.
        self._volume_sizes[str(target_ref)] = size_gb
        return target_ref

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        self._record("upload_to_pool", str(target_ref), str(source_path))
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # idempotent
            path.write_bytes(source_path.read_bytes())
        return target_ref

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        self._record("download_from_pool", str(vol_ref), str(dest_path))
        dest_path.write_bytes(Path(vol_ref).read_bytes())
        return dest_path

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        self._record("delete_volume", str(vol_ref))
        Path(vol_ref).unlink(missing_ok=True)

    # -- VMs ---------------------------------------------------------------

    def create_vm(
        self,
        backend_name: str,
        spec: VMSpec,
        plan_name: str,
        *,
        os_disk_ref: VolumeRef,
        seed_iso_ref: VolumeRef | None,
        network_refs: dict[str, str],
        data_disk_refs: Sequence[VolumeRef] = (),
    ) -> Any:
        del plan_name, os_disk_ref, seed_iso_ref, network_refs
        if self.fail_create_vm:
            raise RuntimeError("simulated create_vm failure")
        self._record("create_vm", backend_name, spec.name, tuple(str(r) for r in data_disk_refs))
        self._vms[backend_name] = "shutoff"
        return f"vm:{backend_name}"

    def start_vm(self, backend_name: str) -> None:
        self._record("start_vm", backend_name)
        self._vms[backend_name] = "running"

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        del timeout
        self._record("shutdown_vm", backend_name)
        self._vms[backend_name] = "shutoff"

    def destroy_vm(self, backend_name: str) -> None:
        self._record("destroy_vm", backend_name)
        self._vms.pop(backend_name, None)

    def get_vm_power_state(self, backend_name: str) -> str:
        del backend_name
        self.power_state_calls += 1
        return "shutoff" if self.power_state_calls >= self.shutoff_after_calls else "running"

    # -- native guest agent (QGA-shaped: unauthenticated, all ops) ---------

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            del timeout, cwd
            self._record("native_guest_execute", backend_name, tuple(argv))
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute

    def native_guest_read_file(self, backend_name: str) -> GuestReadFile:
        def _read_file(path: str) -> bytes:
            self._record("native_guest_read_file", backend_name, path)
            if self.guest_agent_unreachable:
                raise GuestAgentError(f"mock: agent unreachable on {backend_name!r}")
            if path == LEASEFILE:
                if self.sidecar_leases:
                    return self.sidecar_leases.encode("utf-8")
                lines = [f"100 {m} {ip} host *" for m, ip in self._lease_table.items()]
                return ("\n".join(lines) + "\n").encode("utf-8")
            return b"mock-contents"

        return _read_file

    def native_guest_write_file(self, backend_name: str) -> GuestWriteFile:
        def _write_file(path: str, data: bytes) -> None:
            del data
            self._record("native_guest_write_file", backend_name, path)

        return _write_file

    # -- build-result sink -------------------------------------------------

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        # Record eagerly (the generator body is lazy), then replay the canned
        # chunks. A wedge emits no record at all (only heartbeats); a normal
        # build replays the default success token.
        self._record("read_build_result_sink", backend_name)
        chunks = self.build_result_stream
        if chunks is None:
            chunks = () if self.build_result_wedge else _DEFAULT_BUILD_RESULT
        wedge = self.build_result_wedge

        def _stream() -> Generator[bytes, None, None]:
            yield from chunks
            while wedge:  # never emits a record, never EOFs -> watchdog
                yield b""

        return _stream()

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(
        self, vm_backend_name: str, name: str, description: str = "", *, mem: bool = False
    ) -> None:
        self._record("create_snapshot", vm_backend_name, name, description, mem)
        snaps = self._snapshots.setdefault(vm_backend_name, [])
        if name in snaps:
            raise DriverError(f"snapshot {name!r} already exists on vm {vm_backend_name!r}")
        snaps.append(name)

    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        return list(self._snapshots.get(vm_backend_name, []))

    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        self._record("delete_snapshot", vm_backend_name, name)
        snaps = self._snapshots.get(vm_backend_name, [])
        if name in snaps:
            snaps.remove(name)

    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        self._record("restore_snapshot", vm_backend_name, name)
        if name not in self._snapshots.get(vm_backend_name, []):
            raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}")


@dataclass(frozen=True)
class MockProfile(BackendProfile):
    """Connection profile for the in-memory :class:`MockDriver` (CORE-18).

    The mock backend has no real connection; ``pool_root`` and
    ``backing_capacity_gb`` are the only meaningful knobs (mirroring
    :class:`MockHypervisor`), and both default to ``None`` (a per-driver temp
    dir; unlimited capacity).
    """

    scheme: ClassVar[str] = "mock"
    _FIELDS: ClassVar[frozenset[str]] = frozenset({"pool_root", "backing_capacity_gb"})

    pool_root: Path | None = None
    backing_capacity_gb: int | None = None
    build_switch: ManagedBuildSwitch | None = None

    @classmethod
    def _from_table(cls, table: Mapping[str, Any], path: Path) -> Self:
        cls._validate_keys(table, cls._FIELDS, path)
        pool_root = table.get("pool_root")
        capacity = table.get("backing_capacity_gb")
        return cls(
            pool_root=Path(pool_root) if pool_root is not None else None,
            backing_capacity_gb=int(capacity) if capacity is not None else None,
            build_switch=cls._parse_build_switch(table, path),
        )

    def build_driver(self) -> MockDriver:
        return MockDriver(
            pool_root=self.pool_root,
            backing_capacity_gb=self.backing_capacity_gb,
        )

    def describe_fields(self) -> Iterable[tuple[str, str]]:
        if self.pool_root is not None:
            yield ("pool_root", str(self.pool_root))
        if self.backing_capacity_gb is not None:
            yield ("backing_capacity_gb", f"{self.backing_capacity_gb} GiB")


register(
    hypervisor_cls=MockHypervisor,
    driver_name=MockDriver.DRIVER_NAME,
    scheme="mock",
    from_uri=MockDriver.from_uri,
)
register_profile(MockProfile)


__all__ = ["MockDriver", "MockHypervisor", "MockProfile"]
