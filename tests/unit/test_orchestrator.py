"""Tests for the Orchestrator with a fully-mocked driver + cache.

The lifecycle is exercised end-to-end (preflight -> install -> run ->
cleanup) without touching libvirt. Integration coverage on a real
libvirt host lives under tests/integration/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, QGACommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.base import VolumeRef
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.exceptions import (
    BuildNotReadyError,
    InstallTimeoutError,
    OrchestratorError,
    PreflightError,
)
from testrange.networks import Network, Switch
from testrange.networks.sidecar import LEASEFILE
from testrange.orchestrator import Orchestrator
from testrange.packages import Apt
from testrange.preflight import PreflightFinding, PreflightReport
from testrange.vms import VMRecipe, VMSpec


class _FakeDriver:
    """In-memory HypervisorDriver stand-in. Tracks every call for assertions."""

    DRIVER_NAME = "LibvirtDriver"

    def __init__(self, *, uri: str = "fake:///x", pool_root: Path | None = None) -> None:
        self.uri = uri
        self.pool_root = pool_root or Path("/tmp/fake-pools")
        self.connected = False
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.shutoff_after_calls = 1
        self.power_state_calls = 0
        self.preflight_report = PreflightReport()
        self.fail_create_vm = False
        self._pool_dirs: set[Path] = set()
        self._snapshots: dict[str, list[str]] = {}
        # Raw dnsmasq lease-file text the orchestrator will read off the
        # sidecar over QGA during DHCP discovery (see native_guest_read_file).
        # Empty => fall back to the auto-registered _lease_table.
        self.sidecar_leases: str = ""
        self._lease_table: dict[str, str] = {}

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def connect(self) -> None:
        self.connected = True
        self._record("connect")

    def disconnect(self) -> None:
        self.connected = False
        self._record("disconnect")

    def preflight(self, plan: Any, *, cache_manager: Any, install_switch: Any) -> PreflightReport:
        del plan, cache_manager, install_switch
        self._record("preflight")
        return self.preflight_report

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return f"tr_{kind}_{run_id[:8]}_{name}"

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        mac = f"52:54:00:00:{nic_idx:02d}:{abs(hash(vm_name)) % 256:02x}"
        # Auto-register a deterministic lease so sidecar-based DHCP discovery
        # succeeds by default (mirrors the old always-succeeds get_lease_ip);
        # a test that cares about the exact IP sets `sidecar_leases` instead.
        last = int(mac.split(":")[-1], 16) % 254 + 1
        self._lease_table[mac.lower()] = f"10.0.1.{last}"
        return mac

    def compose_volume_ref(self, pool_backend: str, vol_name: str) -> VolumeRef:
        return VolumeRef(str(self.pool_root / pool_backend / vol_name))

    def create_network(
        self,
        network: Any,
        switch: Any,
        backend_name: str,
        *,
        bridge_name: str | None = None,
    ) -> Any:
        self._record("create_network", backend_name, network.name, switch.name, bridge_name)
        return f"net:{backend_name}"

    def destroy_network(self, backend_name: str) -> None:
        self._record("destroy_network", backend_name)

    def compose_bridge_name(self, run_id: str, switch_name: str) -> str:
        return f"tr-{run_id[:6]}-{switch_name}"[:15]

    def create_bridge(self, uplink: str, bridge_name: str, *, mgmt_cidr: str | None = None) -> None:
        self._record("create_bridge", uplink, bridge_name, mgmt_cidr)

    def create_isolated_bridge(self, bridge_name: str, *, mgmt_cidr: str | None = None) -> None:
        self._record("create_isolated_bridge", bridge_name, mgmt_cidr)

    def destroy_bridge(self, bridge_name: str) -> None:
        self._record("destroy_bridge", bridge_name)

    def volume_suffix(self, kind: str) -> str:
        return {
            "install_disk": ".qcow2",
            "run_disk": ".qcow2",
            "base_image": ".qcow2",
            "install_seed": ".iso",
            "sidecar_disk": ".qcow2",
            "sidecar_config": ".iso",
        }[kind]

    def create_pool(self, pool: Any, backend_name: str) -> Any:
        self._record("create_pool", backend_name, pool.name)
        pool_dir = self.pool_root / backend_name
        pool_dir.mkdir(parents=True, exist_ok=True)
        self._pool_dirs.add(pool_dir)
        return f"pool:{backend_name}"

    def destroy_pool(self, backend_name: str) -> None:
        self._record("destroy_pool", backend_name)

    def write_to_pool(self, target_ref: VolumeRef, data: bytes) -> VolumeRef:
        self._record("write_to_pool", str(target_ref), len(data))
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return target_ref

    def create_disk_from_base(
        self,
        target_ref: VolumeRef,
        source_ref: VolumeRef,
    ) -> VolumeRef:
        self._record("create_disk_from_base", str(target_ref), str(source_ref))
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Full-copy semantics: the fake just copies the source bytes.
        path.write_bytes(Path(source_ref).read_bytes())
        return target_ref

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        self._record("upload_to_pool", str(target_ref), str(source_path))
        path = Path(target_ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(source_path.read_bytes())
        return target_ref

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        self._record("download_from_pool", str(vol_ref), str(dest_path))
        dest_path.write_bytes(Path(vol_ref).read_bytes())
        return dest_path

    def delete_volume(self, vol_ref: VolumeRef) -> None:
        self._record("delete_volume", str(vol_ref))
        Path(vol_ref).unlink(missing_ok=True)

    def create_vm(
        self,
        backend_name: str,
        spec: Any,
        plan_name: str,
        *,
        os_disk_ref: VolumeRef,
        seed_iso_ref: VolumeRef | None,
        network_refs: dict[str, str],
    ) -> Any:
        del plan_name, os_disk_ref, seed_iso_ref, network_refs
        if self.fail_create_vm:
            raise RuntimeError("simulated create_vm failure")
        self._record("create_vm", backend_name, spec.name)
        return f"vm:{backend_name}"

    def start_vm(self, backend_name: str) -> None:
        self._record("start_vm", backend_name)

    def get_vm_power_state(self, backend_name: str) -> str:
        self.power_state_calls += 1
        if self.power_state_calls >= self.shutoff_after_calls:
            return "shutoff"
        return "running"

    def native_guest_execute(self, backend_name: str) -> Any:
        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            del timeout, cwd
            self._record("native_guest_execute", backend_name, tuple(argv))
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute

    def native_guest_read_file(self, backend_name: str) -> Any:
        def _read_file(path: str) -> bytes:
            self._record("native_guest_read_file", backend_name, path)
            # The orchestrator reads the sidecar's dnsmasq lease file here for
            # DHCP discovery: explicit sidecar_leases wins, else serve the
            # auto-registered leases so discovery succeeds by default.
            if path == LEASEFILE:
                if self.sidecar_leases:
                    return self.sidecar_leases.encode("utf-8")
                lines = [f"100 {m} {ip} host *" for m, ip in self._lease_table.items()]
                return ("\n".join(lines) + "\n").encode("utf-8")
            return b"fake-contents"

        return _read_file

    def native_guest_write_file(self, backend_name: str) -> Any:
        def _write_file(path: str, data: bytes) -> None:
            del data
            self._record("native_guest_write_file", backend_name, path)

        return _write_file

    def shutdown_vm(self, backend_name: str, *, timeout: float = 120.0) -> None:
        del timeout
        self._record("shutdown_vm", backend_name)

    def destroy_vm(self, backend_name: str) -> None:
        self._record("destroy_vm", backend_name)

    def destroy(self, kind: str, backend_name: str, **metadata: Any) -> None:
        self._record("destroy", kind, backend_name, metadata)
        if kind in ("network", "install_network"):
            self.destroy_network(backend_name)
        elif kind == "pool":
            self.destroy_pool(backend_name)
        elif kind in ("vm", "install_vm", "sidecar_vm"):
            self.destroy_vm(backend_name)
        elif kind in (
            "install_disk",
            "install_seed",
            "run_disk",
            "base_image",
            "sidecar_disk",
            "sidecar_config",
        ):
            self.delete_volume(self.compose_volume_ref(metadata["pool_backend"], backend_name))
        elif kind in ("install_bridge", "bridge"):
            self.destroy_bridge(backend_name)

    def create_snapshot(
        self,
        vm_backend_name: str,
        name: str,
        description: str = "",
        *,
        mem: bool = False,
    ) -> None:
        from testrange.exceptions import DriverError

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
        from testrange.exceptions import DriverError

        self._record("restore_snapshot", vm_backend_name, name)
        if name not in self._snapshots.get(vm_backend_name, []):
            raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}")


def _plan(name: str = "hello") -> Plan:
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.1.0/24", dhcp=True, dns=True),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            LibvirtNetworkIface("netA", addr=DHCPAddr()),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                        packages=[Apt("nginx")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
        name=name,
    )


def _qga_plan(name: str = "hello") -> Plan:
    """Same shape as ``_plan`` but the VM talks over a QGACommunicator."""
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.1.0/24", dhcp=True, dns=True),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            LibvirtNetworkIface("netA", addr=DHCPAddr()),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                        packages=[Apt("nginx")],
                    ),
                    communicator=QGACommunicator(),
                ),
            ],
        ),
        name=name,
    )


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeDriver:
    driver = _FakeDriver(pool_root=tmp_path / "pools")
    monkeypatch.setattr(
        Orchestrator,
        "_build_driver",
        lambda self: driver,
    )
    return driver


@pytest.fixture
def populated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[CacheManager, Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    src = tmp_path / "fake-base.qcow2"
    src.write_bytes(b"FAKE-BASE-DISK" * 100)
    cache.add(src, name="debian-13")
    sidecar = tmp_path / "fake-sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR-DISK" * 100)
    cache.add(sidecar, name="testrange-sidecar")
    return CacheManager(local=cache), tmp_path


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real time.sleep in orchestrator tests."""
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh_execute(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...], float]]:
    """Default SSHCommunicator.execute to a success no-op.

    Lets bring-up paths that call execute (e.g., the builder readiness
    check on a real CloudInitBuilder) complete without real SSH. Tests
    that want to assert what got executed can read the returned list;
    tests that want a failure can override the attribute themselves.
    """
    calls: list[tuple[str, tuple[str, ...], float]] = []

    def fake_execute(
        self: SSHCommunicator,
        argv: Any,
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        calls.append((self.username, tuple(argv), timeout))
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)
    return calls


class TestEnterAndExit:
    def test_full_lifecycle(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr) as orch:
            assert orch.run_id
            assert "web" in orch.vms
        # Both connect + disconnect happened
        names = [c[0] for c in fake_driver.calls]
        assert "connect" in names
        assert "disconnect" in names
        # Install + Run brought up networks/pools/vms
        assert "create_pool" in names
        assert "create_network" in names
        assert "create_vm" in names
        assert "destroy_vm" in names  # run vm torn down on exit
        assert "destroy_network" in names
        assert "destroy_pool" in names

    def test_install_vm_brought_up_and_torn_down(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        # An install_vm was created (cache miss) and destroyed
        install_creates = [
            c for c in fake_driver.calls if c[0] == "create_vm" and "install_vm" in c[1][0]
        ]
        assert len(install_creates) == 1

    def test_cache_hit_skips_install_vm(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        # First run populates the post-install cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        first_install_creates = sum(
            1 for c in fake_driver.calls if c[0] == "create_vm" and "install_vm" in c[1][0]
        )
        assert first_install_creates == 1

        # Reset calls and run again — should hit cache
        fake_driver.calls = []
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        second_install_creates = sum(
            1 for c in fake_driver.calls if c[0] == "create_vm" and "install_vm" in c[1][0]
        )
        assert second_install_creates == 0

    def test_preflight_error_aborts(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        fake_driver.preflight_report = PreflightReport(
            findings=(PreflightFinding(severity="error", code="x", message="nope"),)
        )
        with pytest.raises(PreflightError):
            with Orchestrator(_plan(), cache_manager=mgr):
                pass
        # No state.json was written
        names = [c[0] for c in fake_driver.calls]
        assert "create_pool" not in names

    def test_install_timeout(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        fake_driver.shutoff_after_calls = 99_999  # never goes to shutoff
        # Tiny timeout so the test isn't slow
        with pytest.raises(InstallTimeoutError):
            with Orchestrator(_plan(), cache_manager=mgr, install_timeout_s=0.01):
                pass

    def test_failure_during_bringup_triggers_teardown(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with patch.object(
            fake_driver,
            "create_vm",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                with Orchestrator(_plan(), cache_manager=mgr):
                    pass
        names = [c[0] for c in fake_driver.calls]
        # Pool was created and then destroyed during teardown
        assert "create_pool" in names
        assert "destroy_pool" in names

    def test_no_nics_rejected(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        del fake_driver
        mgr, _ = populated_cache
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24", dhcp=True)],
                pools=[StoragePool("pool1", 32)],
                vms=[
                    VMRecipe(
                        spec=VMSpec(
                            name="web",
                            devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
                        ),
                        builder=CloudInitBuilder(
                            base=CacheEntry("debian-13"),
                            credentials=[PosixCred("u", password="p")],
                        ),
                        communicator=SSHCommunicator("u"),
                    ),
                ],
            ),
            name="hello",
        )
        with pytest.raises(OrchestratorError, match="no NICs"):
            with Orchestrator(plan, cache_manager=mgr):
                pass


class TestStateFileRecord:
    def test_state_dir_removed_after_clean_exit(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        del fake_driver
        mgr, tmp = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr) as orch:
            run_id = orch.run_id
            state_dir = tmp / "s" / "testrange" / "runs" / run_id
            assert (state_dir / "state.json").exists()
        assert not (tmp / "s" / "testrange" / "runs" / run_id).exists()


class TestHandleLeak:
    """``orch.leak()`` on the handle must flip the parent's flag."""

    def test_leak_method_skips_teardown(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        o = Orchestrator(_plan(), cache_manager=mgr)
        with o as orch:
            assert o._leak is False
            orch.leak()
            assert o._leak is True
        # destroy_pool only runs at teardown — leak() must short-circuit it.
        # (destroy_vm and destroy_network both fire mid-install for the
        # transient install resources, so they aren't clean sentinels.)
        names = [c[0] for c in fake_driver.calls]
        assert "destroy_pool" not in names


def _static_plan(ipv4: str) -> Plan:
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            networks=[
                Switch("sw1", Network("netA"), cidr="172.31.0.0/24", dhcp=True),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            LibvirtNetworkIface("netA", addr=StaticAddr(ipv4)),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
        name="hello",
    )


def _two_static_nic_plan(nic_idx: int | None) -> Plan:
    # Two NICs on the SAME network — the case where "by network" is ambiguous
    # and only an index disambiguates the SSH target.
    comm = SSHCommunicator("u", nic_idx=nic_idx) if nic_idx is not None else SSHCommunicator("u")
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            networks=[
                Switch("sw1", Network("netA"), cidr="172.31.0.0/24", dhcp=True),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            LibvirtNetworkIface("netA", addr=StaticAddr("172.31.0.150")),
                            LibvirtNetworkIface("netA", addr=StaticAddr("172.31.0.151")),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=comm,
                ),
            ],
        ),
        name="hello",
    )


class TestNicIdxSelection:
    """SSHCommunicator(nic_idx=) picks which NIC's address to bind to."""

    def test_nic_idx_selects_that_nic(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_two_static_nic_plan(nic_idx=1), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm._host == "172.31.0.151"

    def test_default_binds_first_addressed_nic(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_two_static_nic_plan(nic_idx=None), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm._host == "172.31.0.150"


class TestStaticIPDiscovery:
    """Static-IP NICs short-circuit DHCP lease lookup."""

    def test_static_ip_skips_get_lease_ip(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_static_plan("172.31.0.150"), cache_manager=mgr) as orch:
            web = orch.vms["web"]
            assert isinstance(web.communicator, SSHCommunicator)
            # SSHCommunicator stores the bound host on _host (post-bind).
            assert web.communicator._host == "172.31.0.150"
        # get_lease_ip should not have been called at all — static short-circuits.
        names = [c[0] for c in fake_driver.calls]
        assert "get_lease_ip" not in names

    def test_dhcp_reads_lease_from_sidecar(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        # The default _plan() has a NIC without ipv4 on a dhcp switch, so DHCP
        # discovery must read the lease from the sidecar's dnsmasq lease file
        # (NOT from any hypervisor-managed DHCP).
        mgr, _ = populated_cache
        plan = _plan()
        # Stage a lease for web's first NIC, keyed on its stable MAC.
        mac = fake_driver.compose_mac(plan.name, "web", 0).lower()
        fake_driver.sidecar_leases = f"1700000000 {mac} 10.0.1.55 web *\n"

        with Orchestrator(plan, cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm.host == "10.0.1.55"

        calls = fake_driver.calls
        # The sidecar's lease file was read over the guest-agent transport...
        assert any(c[0] == "native_guest_read_file" and c[1][1] == LEASEFILE for c in calls)
        # ...and the dead hypervisor-side lookup is gone for good.
        assert not any(c[0] == "get_lease_ip" for c in calls)


class TestBuilderReadiness:
    def test_cloudinit_check_runs_after_bind(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
        stub_ssh_execute: list[tuple[str, tuple[str, ...], float]],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        # CloudInitBuilder.wait_ready runs `cloud-init status --wait` once
        # against the bound communicator, with its own inline timeout.
        ready_calls = [c for c in stub_ssh_execute if c[1] == ("cloud-init", "status", "--wait")]
        assert len(ready_calls) == 1
        assert ready_calls[0][2] == 300.0

    def test_failed_check_raises_and_tears_down(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mgr, _ = populated_cache

        def failing_execute(
            self: SSHCommunicator, argv: Any, *, timeout: float = 60.0, cwd: str | None = None
        ) -> ExecResult:
            return ExecResult(exit_code=3, stdout=b"", stderr=b"degraded", duration=0.1)

        monkeypatch.setattr(SSHCommunicator, "execute", failing_execute)
        with pytest.raises(BuildNotReadyError, match="exited 3"):
            with Orchestrator(_plan(), cache_manager=mgr):
                pass
        # Teardown ran even though bring-up failed.
        names = [c[0] for c in fake_driver.calls]
        assert "destroy_vm" in names
        assert "destroy_pool" in names

    def test_builder_noop_skips_check(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
        stub_ssh_execute: list[tuple[str, tuple[str, ...], float]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A builder whose wait_ready is a no-op runs no readiness command.
        monkeypatch.setattr(CloudInitBuilder, "wait_ready", lambda *a, **kw: None)
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        ready_calls = [c for c in stub_ssh_execute if c[1] == ("cloud-init", "status", "--wait")]
        assert ready_calls == []


class TestQGABinding:
    def test_qga_plan_binds_via_driver(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_qga_plan(), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, QGACommunicator)
            assert comm.is_bound is True
        # The QGA path never discovers an IP — get_lease_ip is SSH-only.
        assert not any(c[0] == "get_lease_ip" for c in fake_driver.calls)
        # Builder readiness ran `cloud-init status --wait` through the agent.
        agent_calls = [c for c in fake_driver.calls if c[0] == "native_guest_execute"]
        assert any(c[1][1] == ("cloud-init", "status", "--wait") for c in agent_calls)

    def test_qga_communicator_execute_reaches_driver(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_qga_plan(), cache_manager=mgr) as orch:
            r = orch.vms["web"].communicator.execute(["id", "-u"])
        assert r.exit_code == 0
        assert any(
            c[0] == "native_guest_execute" and c[1][1] == ("id", "-u") for c in fake_driver.calls
        )
