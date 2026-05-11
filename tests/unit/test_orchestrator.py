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
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, LibvirtNetworkIface, Memory, OSDrive, StoragePool
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.exceptions import (
    InstallTimeoutError,
    OrchestratorError,
    PreflightError,
)
from testrange.networks import Network, Switch
from testrange.orchestrator import Orchestrator
from testrange.packages import Apt
from testrange.preflight import PreflightFinding, PreflightReport
from testrange.vms import VMRecipe, VMSpec

# ---- fakes -----------------------------------------------------------------


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

    # ---- bookkeeping ---------------------------------------------------

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    # ---- ABC surface --------------------------------------------------

    def connect(self) -> None:
        self.connected = True
        self._record("connect")

    def disconnect(self) -> None:
        self.connected = False
        self._record("disconnect")

    def preflight(self, plan: Any, *, cache_manager: Any) -> PreflightReport:
        del plan, cache_manager
        self._record("preflight")
        return self.preflight_report

    def compose_resource_name(self, run_id: str, kind: str, name: str) -> str:
        return f"tr_{kind}_{run_id[:8]}_{name}"

    def compose_mac(self, plan_name: str, vm_name: str, nic_idx: int) -> str:
        return f"52:54:00:00:{nic_idx:02d}:{abs(hash(vm_name)) % 256:02x}"

    def create_network(self, network: Any, switch: Any, backend_name: str) -> Any:
        self._record("create_network", backend_name, network.name, switch.name)
        return f"net:{backend_name}"

    def destroy_network(self, backend_name: str) -> None:
        self._record("destroy_network", backend_name)

    def create_pool(self, pool: Any, backend_name: str) -> Any:
        self._record("create_pool", backend_name, pool.name)
        pool_dir = self.pool_root / backend_name
        pool_dir.mkdir(parents=True, exist_ok=True)
        self._pool_dirs.add(pool_dir)
        return f"pool:{backend_name}"

    def destroy_pool(self, backend_name: str) -> None:
        self._record("destroy_pool", backend_name)

    def write_to_pool(self, pool_backend: str, filename: str, data: bytes) -> Path:
        self._record("write_to_pool", pool_backend, filename, len(data))
        path = self.pool_root / pool_backend / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def create_overlay_disk(
        self,
        pool_backend: str,
        vol_name: str,
        source_path: Path,
    ) -> Path:
        self._record("create_overlay_disk", pool_backend, vol_name, str(source_path))
        path = self.pool_root / pool_backend / vol_name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a marker; the cache happily ingests any bytes.
        path.write_bytes(b"FAKE-OVERLAY:" + source_path.name.encode())
        return path

    def delete_volume(self, pool_backend: str, vol_name: str) -> None:
        self._record("delete_volume", pool_backend, vol_name)
        path = self.pool_root / pool_backend / vol_name
        path.unlink(missing_ok=True)

    def create_vm(
        self,
        backend_name: str,
        spec: Any,
        plan_name: str,
        *,
        os_disk_path: Path,
        seed_iso_path: Path | None,
        network_refs: dict[str, str],
    ) -> Any:
        del plan_name, os_disk_path, seed_iso_path, network_refs
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
        elif kind in ("vm", "install_vm"):
            self.destroy_vm(backend_name)
        elif kind in ("install_disk", "install_seed", "run_disk"):
            self.delete_volume(metadata["pool_backend"], backend_name)


# ---- fixtures --------------------------------------------------------------


def _plan(name: str = "hello") -> Plan:
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            networks=[
                Switch("sw1", Network("netA", "10.0.1.0/24", dhcp=True, dns=True)),
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
                            LibvirtNetworkIface("netA"),
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
    return CacheManager(local=cache), tmp_path


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real time.sleep in orchestrator tests."""
    monkeypatch.setattr("testrange.orchestrator.runtime.time.sleep", lambda _s: None)


# ---- tests -----------------------------------------------------------------


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
            c for c in fake_driver.calls
            if c[0] == "create_vm" and "install_vm" in c[1][0]
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
            1 for c in fake_driver.calls
            if c[0] == "create_vm" and "install_vm" in c[1][0]
        )
        assert first_install_creates == 1

        # Reset calls and run again — should hit cache
        fake_driver.calls = []
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        second_install_creates = sum(
            1 for c in fake_driver.calls
            if c[0] == "create_vm" and "install_vm" in c[1][0]
        )
        assert second_install_creates == 0

    def test_preflight_error_aborts(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        fake_driver.preflight_report = PreflightReport(
            findings=(
                PreflightFinding(severity="error", code="x", message="nope"),
            )
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
                networks=[Switch("sw1", Network("netA", "10.0.1.0/24"))],
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


class TestRunTests:
    def test_run_tests_brings_up_and_tears_down(
        self,
        fake_driver: _FakeDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        del fake_driver
        mgr, _ = populated_cache
        from testrange.orchestrator import run_tests

        def my_test(orch):  # type: ignore[no-untyped-def]
            pass

        results = run_tests([my_test], _plan(), cache_manager=mgr)
        assert len(results) == 1
        assert results[0].name == "my_test"
        # Phase 5 will replace the placeholder
        assert "Phase 5" in (results[0].error or "")
