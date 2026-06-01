"""Run-phase bring-up runs VMs concurrently (ADR-0020, ORCH-18).

Drives :func:`run_phase` directly against a bare (sidecar-less, static-IP)
multi-VM plan so the only work is the per-VM disk upload + create + start. A
mock driver whose ``upload_to_pool`` sleeps proves the uploads overlap, and the
state ledger is asserted complete + uncorrupted under the thread pool.
"""

from __future__ import annotations

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
from testrange.state.store import StateStore, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor

_UPLOAD_DELAY_S = 0.05


class _SlowUploadDriver(MockDriver):
    """MockDriver whose disk upload blocks, so overlap is measurable."""

    def upload_to_pool(self, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
        time.sleep(_UPLOAD_DELAY_S)
        return super().upload_to_pool(target_ref, source_path)


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

        start = time.monotonic()
        run_phase(ctx)
        elapsed = time.monotonic() - start
        # 4 VMs x one 50ms upload each = 200ms serial; overlapped on 4 workers it
        # is ~50ms. Generous bound to avoid CI flakiness while still failing if
        # the loop runs serially.
        assert elapsed < 0.15, f"expected overlapped uploads, took {elapsed:.3f}s"

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
