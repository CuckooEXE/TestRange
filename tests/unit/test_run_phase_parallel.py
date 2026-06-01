"""Run-phase bring-up runs VMs concurrently (ADR-0020, ORCH-18).

Drives :func:`run_phase` directly against a bare (sidecar-less, static-IP)
multi-VM plan so the only work is the per-VM disk upload + create + start. A
mock driver whose ``upload_to_pool`` sleeps proves the uploads overlap, and the
state ledger is asserted complete + uncorrupted under the thread pool.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.base import VolumeRef
from testrange.exceptions import DriverError
from testrange.networks import Network, Switch
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.run_phase import run_phase
from testrange.orchestrator.teardown import teardown
from testrange.state.store import StateStore, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor

_UPLOAD_DELAY_S = 0.05


class _SlowUploadDriver(MockDriver):
    """MockDriver whose disk upload blocks and records peak concurrency.

    ``max_in_flight`` lets a test assert genuine overlap (>= 2 uploads running
    at once) instead of a flaky wall-clock threshold.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            time.sleep(_UPLOAD_DELAY_S)
            return super().upload_to_pool(target_ref, source_path)
        finally:
            with self._lock:
                self._in_flight -= 1


class _FailingUploadDriver(MockDriver):
    """Fails the upload for exactly one VM's OS disk."""

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        if "vm2" in str(target_ref):
            raise DriverError("simulated upload failure on vm2")
        return super().upload_to_pool(target_ref, source_path)


def _bare_plan(n_vms: int) -> Plan:
    """``n_vms`` static-IP VMs on one sidecar-less switch + one pool."""
    vms = [
        VMRecipe(
            spec=VMSpec(
                name=f"vm{i}",
                devices=[
                    CPU(1),
                    Memory(256),
                    OSDrive("pool1", 8),
                    NetworkIface("netA", addr=StaticAddr(f"10.0.1.{100 + i}")),
                ],
            ),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"),
                credentials=[PosixCred("u", password="p")],
            ),
            communicator=SSHCommunicator("u"),
        )
        for i in range(n_vms)
    ]
    return Plan(
        "p",
        MockHypervisor(
            networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24")],
            pools=[StoragePool("pool1", 256)],
            vms=vms,
        ),
    )


def _ctx(plan: Plan, driver: MockDriver, tmp_path: Path) -> RunContext:
    run_id = "r1"
    store = StateStore(run_dir_for(run_id, root=tmp_path / "state"))
    store.initialize(run_id=run_id, plan_name="p", driver_class="MockDriver", driver_uri="")
    built = tmp_path / "built.qcow2"
    built.write_bytes(b"BUILT-OS-DISK")
    switches = plan.hypervisor.networks
    return RunContext(
        plan=plan,
        resolved=ResolvedBackend(driver=driver, driver_uri=""),
        store=store,
        cache=CacheManager(local=LocalCache(root=tmp_path / "cache")),
        run_id=run_id,
        plan_name="p",
        build_timeout_s=60.0,
        lease_timeout_s=60.0,
        addressing={n.name: NetworkAddressing.from_switch(s) for s in switches for n in s.networks},
        built_disk_paths={vm.name: {"os": built} for vm in plan.hypervisor.vms},
    )


class TestRunPhaseParallel:
    def test_all_vms_brought_up(self, tmp_path: Path) -> None:
        driver = _SlowUploadDriver(pool_root=tmp_path / "pools")
        plan = _bare_plan(4)
        run_phase(_ctx(plan, driver, tmp_path))

        started = {c[1][0] for c in driver.calls if c[0] == "start_vm"}
        assert started == {driver.compose_resource_name("r1", "vm", f"vm{i}") for i in range(4)}

    def test_uploads_overlap(self, tmp_path: Path) -> None:
        driver = _SlowUploadDriver(pool_root=tmp_path / "pools")
        plan = _bare_plan(4)
        ctx = _ctx(plan, driver, tmp_path)

        run_phase(ctx)
        # Structural overlap assertion (not wall-clock): at least two per-VM
        # uploads were in flight simultaneously, proving the phase parallelized
        # rather than ran serially. Robust on a loaded/throttled CI box.
        assert driver.max_in_flight >= 2, (
            f"expected overlapped uploads, peak in-flight was {driver.max_in_flight}"
        )

    def test_ledger_complete_under_concurrency(self, tmp_path: Path) -> None:
        driver = _SlowUploadDriver(pool_root=tmp_path / "pools")
        plan = _bare_plan(6)
        ctx = _ctx(plan, driver, tmp_path)
        run_phase(ctx)

        resources = {r.backend_name: r for r in ctx.store.read().resources}
        # Every VM, its run disk, the pool, switch and network are recorded, and
        # all confirmed (outcome_at set) — no lost writes from the RMW race.
        for i in range(6):
            vm_backend = driver.compose_resource_name("r1", "vm", f"vm{i}")
            assert vm_backend in resources
            assert any(
                name.startswith(vm_backend) and res.kind == "run_disk"
                for name, res in resources.items()
            )
        assert all(r.outcome_at is not None for r in resources.values())

    def test_one_upload_failure_aborts(self, tmp_path: Path) -> None:
        driver = _FailingUploadDriver(pool_root=tmp_path / "pools")
        plan = _bare_plan(4)
        ctx = _ctx(plan, driver, tmp_path)

        with pytest.raises(DriverError, match="vm2"):
            run_phase(ctx)
        # State is still readable (not torn) after the aborted phase.
        assert ctx.store.read().resources is not None

    def test_partial_failure_leaves_no_leak_after_teardown(self, tmp_path: Path) -> None:
        # The real risk of a *parallel* partial failure: a sibling VM that
        # succeeded (or was mid-flight) before vm2 raised must still be recorded
        # in state so teardown reaches it — no orphaned backend resource. Drive
        # the failure, then run the ledger-driven teardown and assert the mock
        # backend holds nothing live.
        driver = _FailingUploadDriver(pool_root=tmp_path / "pools")
        ctx = _ctx(_bare_plan(4), driver, tmp_path)

        with pytest.raises(DriverError, match="vm2"):
            run_phase(ctx)

        # Every resource the surviving siblings created is recorded (record-
        # before-create), so it is reachable for teardown — nothing leaks
        # silently outside the ledger.
        recorded = {r.backend_name for r in ctx.store.read().resources}
        assert driver._pools <= recorded
        assert set(driver._vms) <= recorded

        teardown(ctx)

        # Backend is empty: every created pool / switch / network / VM destroyed.
        assert driver._pools == set(), f"leaked pools: {driver._pools}"
        assert driver._vms == {}, f"leaked VMs: {driver._vms}"
        assert driver._switches == {}, f"leaked switches: {driver._switches}"
        assert driver._networks == {}, f"leaked networks: {driver._networks}"
