"""Parallel materialize walk: concurrent VM builds with distinct build IPs (ORCH-4).

Covers the deterministic per-VM build-switch address allocation
(:func:`_build_ip_offset`) and an end-to-end multi-miss materialize on the mock
driver. MVP graphs carry only ordering edges, so every VM node lands in the
first content wave and all cache-missing VMs build concurrently: all disks
captured, capture downloads overlap, and a single VM's build failure aborts
the walk.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.base import VolumeRef
from testrange.exceptions import BuildFailedError, OrchestratorError
from testrange.networks import Network, Sidecar, Switch
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.executor import materialize_graph
from testrange.orchestrator.vm_build import _BUILD_IP_SLOTS, _build_ip_offset
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor

_DOWNLOAD_DELAY_S = 0.05


class TestBuildIpOffset:
    def test_infra_range_first(self) -> None:
        # .3-.9 are the seven infra slots, used before anything else.
        assert [_build_ip_offset(i) for i in range(7)] == [3, 4, 5, 6, 7, 8, 9]

    def test_skips_dhcp_pool(self) -> None:
        # After .9 the allocator jumps past the sidecar DHCP pool (.10-.99).
        assert _build_ip_offset(7) == 100
        assert _build_ip_offset(8) == 101

    def test_distinct_per_index(self) -> None:
        offsets = [_build_ip_offset(i) for i in range(50)]
        assert len(set(offsets)) == 50

    def test_exhaustion_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="exceeds"):
            _build_ip_offset(len(_BUILD_IP_SLOTS))


class _SlowDownloadDriver(MockDriver):
    """Capture download blocks and records peak concurrency across VMs."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def download_from_pool(self, vol_ref: VolumeRef, dest_path: Path) -> Path:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(_DOWNLOAD_DELAY_S)
            return super().download_from_pool(vol_ref, dest_path)
        finally:
            with self._lock:
                self._in_flight -= 1


class _FailOneBuildDriver(MockDriver):
    """The build VM whose backend name contains ``vm2`` reports a failure."""

    def read_build_result_sink(self, backend_name: str) -> Generator[bytes, None, None]:
        if "vm2" in backend_name:
            self.build_result_stream = (b'TESTRANGE-RESULT: fail rc=1 cmd="apt-get"\n',)
        else:
            self.build_result_stream = None
        return super().read_build_result_sink(backend_name)


def _multi_plan(n_vms: int) -> Plan:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 256))
    hyp.add_switch(
        Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True))
    )
    for i in range(n_vms):
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name=f"vm{i}",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive(hyp.pools["pool1"], 8),
                        NetworkIface(hyp.networks["netA"], addr=DHCPAddr()),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[PosixCred("u", password="p")],
                ),
                communicator=SSHCommunicator("u"),
            )
        )
    return Plan("multi", hyp)


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: MockDriver) -> CacheManager:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"FAKE-BASE" * 100)
    cache.add(base, name="debian-13")
    sidecar = tmp_path / "sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR" * 100)
    cache.add(sidecar, name="testrange-sidecar")
    driver.connect()
    return CacheManager(local=cache)


def _ctx(plan: Plan, driver: MockDriver, cache: CacheManager) -> GraphContext:
    run_id = new_run_id()
    store = StateStore(run_dir_for(run_id))
    store.initialize(run_id=run_id, plan_name=plan.name, driver_class="MockDriver", driver_uri="")
    switches = plan.hypervisor.declared_switches
    return GraphContext(
        plan=plan,
        resolved=ResolvedBackend(driver=driver, driver_uri=""),
        store=store,
        cache=cache,
        run_id=run_id,
        plan_name=plan.name,
        build_timeout_s=5.0,
        lease_timeout_s=5.0,
        addressing={n.name: NetworkAddressing.from_switch(s) for s in switches for n in s.networks},
    )


class TestParallelMaterialize:
    def test_all_vms_captured_with_distinct_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = MockDriver(pool_root=tmp_path / "pools")
        cache = _env(tmp_path, monkeypatch, driver)
        plan = _multi_plan(4)
        ctx = _ctx(plan, driver, cache)
        materialize_graph(ctx, plan.graph)

        # Every VM produced a captured OS disk path...
        assert set(ctx.built_disk_paths) == {f"vm{i}" for i in range(4)}
        # ...and each landed under a distinct config_hash (the per-VM build IP
        # feeds config_hash, so otherwise-identical VMs do not collide).
        built_os = sorted(
            name
            for info in cache.local.list_entries()
            for name in info.names
            if name.startswith("_built_") and name.endswith("__os")
        )
        assert len(built_os) == 4

    def test_capture_downloads_overlap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _SlowDownloadDriver(pool_root=tmp_path / "pools")
        cache = _env(tmp_path, monkeypatch, driver)
        plan = _multi_plan(4)
        ctx = _ctx(plan, driver, cache)

        materialize_graph(ctx, plan.graph)
        # Structural overlap (not wall-clock): at least two capture downloads ran
        # at once, proving the materialize wave overlapped them.
        assert driver.max_in_flight >= 2, (
            f"expected overlapped capture, peak in-flight was {driver.max_in_flight}"
        )

    def test_one_build_failure_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = _FailOneBuildDriver(pool_root=tmp_path / "pools")
        cache = _env(tmp_path, monkeypatch, driver)
        plan = _multi_plan(4)
        ctx = _ctx(plan, driver, cache)

        with pytest.raises(BuildFailedError, match="vm2"):
            materialize_graph(ctx, plan.graph)
