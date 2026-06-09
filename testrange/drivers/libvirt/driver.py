"""libvirt backend driver — assembled keystone (BACKEND-1).

``LibvirtHypervisor`` is the Plan-time entry selecting this backend;
``LibvirtDriver`` is the concrete :class:`HypervisorDriver`. The driver is the
single place the full ABC surface is assembled; it holds exactly one
:class:`LibvirtClient` and delegates every method to a focused concern module,
mirroring the Proxmox split:

- ``_conn`` — the libvirt-python connection (``qemu:///system``);
- ``_naming`` — pure deterministic resource names, MACs, and volume refs;
- ``_net`` — L2 fabric via the libvirt network API (isolated segment + the
  resolved host-bridge uplink segment for a NAT sidecar);
- ``_storage`` — per-run dir pools + stream volume I/O;
- ``_vm`` — VM lifecycle (domain XML) and snapshots;
- ``_guest`` — the QGA native guest agent (exec / read / write) over
  ``libvirt_qemu.qemuAgentCommand``;
- ``_serial`` — the build-result sink (the ``<serial type='unix'>`` socket).

Every concern is implemented and delegated; the driver is the certified
reference backend (ADR-0019).
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers._registry import register
from testrange.drivers.base import HypervisorDriver, VolumeRef
from testrange.drivers.libvirt import _guest, _naming, _net, _serial, _storage, _vm
from testrange.drivers.libvirt._conn import LibvirtClient, LibvirtConn
from testrange.gateways import SSHJumpGateway
from testrange.hypervisor import Hypervisor
from testrange.preflight import (
    HostCapacity,
    PreflightCheck,
    PreflightFinding,
    PreflightReport,
    builder_origin_findings,
    preflight_switches,
    resource_check,
    unknown_uplink_findings,
    unsupported_firmware_findings,
)

_log = get_logger(__name__)


def _probe_host_nested_kvm() -> bool | None:
    """Tri-state probe of the local host's KVM-nesting toggle.

    Returns ``True`` when a ``kvm_intel``/``kvm_amd`` ``nested`` parameter reads
    as on (``Y``/``1``), ``False`` when one reads as explicitly off (``N``/``0``),
    and ``None`` when neither file is readable or carries a recognized value
    (module not loaded yet, empty transient read, exotic kernel). ``None`` is
    *indeterminate* — the caller must not turn it into a hard preflight reject,
    only an explicit ``False`` is a real "nesting is off" signal. Module-level so
    a preflight test can monkeypatch it.
    """
    for module in ("kvm_intel", "kvm_amd"):
        try:
            head = Path(f"/sys/module/{module}/parameters/nested").read_text().strip()[:1]
        except OSError:
            continue
        if head in ("Y", "1"):
            return True
        if head in ("N", "0"):
            return False
    return None


if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.manager import CacheManager
    from testrange.credentials.base import Credential
    from testrange.devices.pool.base import StoragePool
    from testrange.gateways.base import GuestGateway
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile
    from testrange.networks.base import BuildNic, Network, Switch
    from testrange.plan import Plan
    from testrange.vms.spec import VMSpec


@dataclass(frozen=True)
class LibvirtHypervisor(Hypervisor):
    """Topology-only scheme marker selecting the ``libvirt`` backend (CORE-19).

    Identical in shape to the generic :class:`~testrange.Hypervisor`; its only
    job is to assert *this topology MUST run against libvirt* (e.g., a recipe
    that relies on a libvirt-specific NIC model) so preflight catches a
    mismatched ``--profile`` early. Connection (``uri``) and named uplinks live
    on :class:`LibvirtProfile`.
    """


class LibvirtDriver(HypervisorDriver):
    """libvirt backend. Holds exactly one :class:`LibvirtClient`."""

    DRIVER_NAME = "LibvirtDriver"

    # Firmware this backend realizes (BUILD-1b): bios via SeaBIOS on `pc`, uefi
    # via OVMF (libvirt's `firmware='efi'` auto-descriptor) on `q35` — see _vm._os_xml.
    SUPPORTED_FIRMWARES = frozenset({"bios", "uefi"})

    def __init__(
        self,
        conn: LibvirtConn,
        *,
        client: LibvirtClient | None = None,
        uplinks: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        # ``client`` is injectable so unit tests pass a duck-typed fake (exposing
        # ``raw`` + the libvirt calls the concern modules use) and never import
        # libvirt or touch a real hypervisor.
        self._client = client if client is not None else LibvirtClient(conn)
        # Logical-uplink-name → host bridge (ADR-0016), from the profile. A
        # resolved uplink names an existing host network (e.g. tr-egress). Empty
        # for a from_uri teardown driver (it never wires NICs / creates switches).
        self._uplinks: dict[str, str] = dict(uplinks or {})
        # Composed network backend name → the switch's libvirt network name,
        # populated in create_network. The orchestrator passes the composed name
        # in network_refs, but a NIC attaches to the switch's shared libvirt
        # network (all Networks on a Switch share one bridge), so create_vm
        # translates through this. The uplink network (e.g. tr-egress) isn't in
        # the map and passes through unchanged. In-process only: teardown never
        # wires NICs.
        self._libvirt_net_by_network: dict[str, str] = {}
        # Guards the map above. The run phase provisions independent switches
        # concurrently (ADR-0023), so create_network writes it from several
        # worker threads and create_vm reads it; the lock makes the write
        # visible and the read consistent rather than leaning on dict-op GIL
        # atomicity (which the ADR itself flags as fragile). Held only for the
        # dict access — the libvirt network API calls run unlocked on the
        # internally-thread-safe virConnect.
        self._state_lock = threading.Lock()

    @classmethod
    def from_uri(cls, uri: str) -> LibvirtDriver:
        return cls(LibvirtConn.from_uri(uri))

    @property
    def uri(self) -> str:
        return self._conn.to_uri()

    def connect(self) -> None:
        self._client.connect()

    def disconnect(self) -> None:
        self._client.close()

    def guest_gateway(self) -> GuestGateway | None:
        """SSH-jump through the ``qemu+ssh`` host for a remote libvirt (ADR-0021).

        Local libvirt (``qemu:///system``) — guests are directly routable from the
        co-located orchestrator, so ``None`` (unchanged). A **remote** ``qemu+ssh``
        connection — the binding a nested guest hypervisor is reached over — puts
        its guests on the remote host's *internal* networks the orchestrator
        cannot route to. An ``SSHCommunicator`` reaches them by tunnelling through
        the same host + key the connect URI already carries (the depth-2 nested
        wall; also any SSH-communicator inner VM). QGA transports ride the libvirt
        control plane and ignore this.
        """
        parsed = urllib.parse.urlparse(self._conn.libvirt_uri)
        if not parsed.scheme.startswith("qemu+ssh") or not parsed.hostname:
            return None
        keyfile = urllib.parse.parse_qs(parsed.query).get("keyfile", [None])[0]
        pkey_text = Path(keyfile).read_text(encoding="utf-8") if keyfile else None
        return SSHJumpGateway(
            host=parsed.hostname,
            username=parsed.username or "root",
            pkey_text=pkey_text,
            port=parsed.port or 22,
        )

    def preflight(
        self, plan: Plan, *, cache_manager: CacheManager, build_switch: Switch | None
    ) -> PreflightReport:
        """Plan-side read-only checks (the named-uplink resolution check, ADR-0016).

        libvirt **supports** ``Switch(mgmt=True)``: the daemon puts the host's
        ``.2`` adapter on the bridge (``_net.create_switch``), which is how the
        on-host orchestrator reaches an SSH-communicator VM on a local network.
        So libvirt does NOT call ``mgmt_unsupported_findings`` — it is the
        backend that realizes mgmt (the "drops the call" path other backends
        still defer pending ADR-0009). Live libvirt-side checks (the resolved
        uplink network present on the host) land with the L2 phase.
        """
        del cache_manager
        # build_switch is None for a cache-only run (require_cache) that never
        # realizes it; preflight_switches drops it from the sweep (CORE-65).
        switches = preflight_switches(plan, build_switch)
        return PreflightReport.from_checks(
            [
                PreflightCheck.evaluate(
                    "named-uplink-resolution", unknown_uplink_findings(switches, self._uplinks)
                ),
                PreflightCheck.evaluate("os-disk-origin", builder_origin_findings(plan)),
                PreflightCheck.evaluate(
                    "supported-firmware",
                    unsupported_firmware_findings(
                        plan, self.SUPPORTED_FIRMWARES, driver_name=self.DRIVER_NAME
                    ),
                ),
                PreflightCheck.evaluate("nested-kvm", self._nested_kvm_findings(plan)),
                resource_check(plan, self.host_capacity()),
            ]
        )

    def host_capacity(self) -> HostCapacity | None:
        """Total RAM + logical CPUs of the libvirt host (CORE-84).

        ``virConnect.getInfo()`` returns ``[model, memory_MiB, cpus, ...]`` for the
        daemon's host — the right ceiling for both local ``qemu:///system`` and a
        remote ``qemu+ssh`` daemon. Best-effort: any failure (not connected,
        transport error) returns ``None`` so a flaky probe never blocks a run.
        """
        try:
            info = self._client.raw.getInfo()
        except Exception as e:  # libvirtError / DriverError(not connected) / ...
            _log.debug("libvirt host_capacity probe failed: %s", e)
            return None
        return HostCapacity(memory_mb=int(info[1]), logical_cpus=int(info[2]))

    def _nested_kvm_findings(self, plan: Plan) -> tuple[PreflightFinding, ...]:
        """Reject ``CPU(nested=True)`` when the L0 host can't run nested guests (ADR-0021).

        A nested hypervisor only boots its inner VMs with KVM if the host exposes
        the virtualization extensions (``vmx``/``svm``) — which libvirt's
        ``host-passthrough`` forwards only when KVM nesting is on. We verify the
        host module parameter and fail loud here rather than let an inner VM crawl
        under TCG emulation. Only checkable when libvirtd is **local** (the URI
        names no host): reading ``/sys/module/kvm_*/parameters/nested`` is a
        host-filesystem read, and a remote daemon's sysfs isn't reachable over the
        libvirt API (a real remote probe via the capabilities API is deferred with
        the rest of the remote surface, BACKEND-5). For a remote L0 we therefore
        cannot verify nesting — we ``warning``-log the gap rather than skip it
        silently, because that is precisely the case an inner VM degrades to TCG.
        """
        if not any(vm.spec.cpu.nested for vm in plan.hypervisor.vms):
            return ()
        host = urllib.parse.urlparse(self._conn.libvirt_uri).hostname
        if host not in (None, "", "localhost"):
            _log.warning(
                "nested CPU requested but libvirt is remote (%s); cannot verify host "
                "nested-KVM over the API yet (BACKEND-5) — inner guests will fall back "
                "to slow TCG emulation if that host has KVM nesting disabled",
                self._conn.libvirt_uri,
            )
            return ()
        state = _probe_host_nested_kvm()
        if state is None:
            _log.warning(
                "nested CPU requested but the host's KVM-nesting state is indeterminate "
                "(no readable /sys/module/kvm_{intel,amd}/parameters/nested); proceeding "
                "without the preflight check — inner guests will fall back to slow TCG "
                "emulation if nesting is in fact off"
            )
            return ()
        if state:
            return ()
        return (
            PreflightFinding(
                code="nested-kvm-disabled",
                message=(
                    "a VM declares CPU(nested=True) but the host has KVM nesting disabled "
                    "(/sys/module/kvm_{intel,amd}/parameters/nested is not Y); inner guests "
                    "would fall back to slow TCG emulation or fail to boot"
                ),
                fix_hint=(
                    "enable nesting: `echo Y | sudo tee /sys/module/kvm_intel/parameters/nested` "
                    "(or kvm_amd), or set `options kvm_intel nested=1` in /etc/modprobe.d and "
                    "reload the module"
                ),
            ),
        )

    def _resolve_uplink(self, switch: Switch) -> str | None:
        """The host bridge ``switch.uplink`` maps to, or ``None`` if it declares none.

        A declared-but-unmapped name is left to surface in the concern module
        (preflight already flags it via ``unknown_uplink_findings``).
        """
        if switch.uplink is None:
            return None
        return self._uplinks.get(switch.uplink)

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return _naming.compose_resource_name(run_id, kind, name)

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return _naming.compose_mac(plan_name, vm_name, nic_idx)

    def compose_volume_ref(self, pool_backend_name: str, vol_name: str) -> VolumeRef:
        return _naming.compose_volume_ref(pool_backend_name, vol_name)

    def volume_suffix(self, kind: str) -> str:
        return _naming.volume_suffix(kind)

    def create_switch(self, switch: Switch, backend_name: str) -> str | None:
        return _net.create_switch(
            self._client, switch, backend_name, resolved_uplink=self._resolve_uplink(switch)
        )

    def destroy_switch(self, backend_name: str) -> None:
        _net.destroy_switch(self._client, backend_name)

    def create_network(
        self, network: Network, switch: Switch, backend_name: str, *, switch_backend_name: str
    ) -> Any:
        libvirt_net = _net.create_network(
            self._client, network, switch, backend_name, switch_backend_name=switch_backend_name
        )
        # Remember composed name → switch's libvirt network so create_vm wires
        # NICs onto the real (shared) network, not the composed alias.
        with self._state_lock:
            self._libvirt_net_by_network[backend_name] = libvirt_net
        return libvirt_net

    def destroy_network(self, backend_name: str) -> None:
        _net.destroy_network(self._client, backend_name)

    def create_pool(self, pool: StoragePool, backend_name: str) -> Any:
        return _storage.create_pool(self._client, pool, backend_name)

    def destroy_pool(self, backend_name: str) -> None:
        _storage.destroy_pool(self._client, backend_name)

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        return _storage.write_to_pool(self._client, target_ref, data)

    def create_blank_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.create_blank_volume(self._client, target_ref, size_gb)

    def resize_volume(self, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
        return _storage.resize_volume(self._client, target_ref, size_gb)

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        return _storage.upload_to_pool(self._client, target_ref, source_path)

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        return _storage.download_from_pool(self._client, vol_ref, dest_path)

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        _storage.delete_volume(self._client, vol_ref)

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
        build_nic: BuildNic | None = None,
        boot_media_ref: VolumeRef | None = None,
    ) -> Any:
        # Translate composed network names → the switch's shared libvirt network;
        # the uplink network (e.g. tr-egress) isn't in the map and passes through.
        with self._state_lock:
            resolved_refs = {
                name: self._libvirt_net_by_network.get(backend, backend)
                for name, backend in network_refs.items()
            }
        return _vm.create_vm(
            self._client,
            backend_name,
            spec,
            plan_name,
            os_disk_ref=os_disk_ref,
            seed_iso_ref=seed_iso_ref,
            network_refs=resolved_refs,
            data_disk_refs=data_disk_refs,
            build_nic=build_nic,
            boot_media_ref=boot_media_ref,
        )

    def start_vm(self, backend_name: str) -> None:
        _vm.start_vm(self._client, backend_name)

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        _vm.shutdown_vm(self._client, backend_name, timeout=timeout)

    def destroy_vm(self, backend_name: str) -> None:
        _vm.destroy_vm(self._client, backend_name)

    def get_vm_power_state(self, backend_name: str) -> str:
        return _vm.get_vm_power_state(self._client, backend_name)

    def native_guest_execute(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestExec:
        del credential  # QGA authenticates at the channel; no per-call guest login
        return _guest.make_execute(self._client, backend_name)

    def native_guest_read_file(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestReadFile:
        del credential
        return _guest.make_read_file(self._client, backend_name)

    def native_guest_write_file(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestWriteFile:
        del credential
        return _guest.make_write_file(self._client, backend_name)

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        return _serial.read_build_result_sink(self._client, backend_name)

    def create_snapshot(
        self, vm_backend_name: str, name: str, description: str = "", *, mem: bool = False
    ) -> None:
        _vm.create_snapshot(self._client, vm_backend_name, name, description, mem=mem)

    def list_snapshots(self, vm_backend_name: str) -> list[str]:
        return _vm.list_snapshots(self._client, vm_backend_name)

    def delete_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.delete_snapshot(self._client, vm_backend_name, name)

    def restore_snapshot(self, vm_backend_name: str, name: str) -> None:
        _vm.restore_snapshot(self._client, vm_backend_name, name)


register(
    hypervisor_cls=LibvirtHypervisor,
    driver_name=LibvirtDriver.DRIVER_NAME,
    scheme="libvirt",
    from_uri=LibvirtDriver.from_uri,
)


__all__ = ["LibvirtDriver", "LibvirtHypervisor"]
