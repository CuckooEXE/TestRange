"""Per-VM readiness waits overlap across the realize wave (ADR-0023, ORCH-19).

Under the DAG executor the communicator-readiness gate runs *inside* each VM
node's realize (ADR-0030); VM nodes in one wave realize on the thread pool, so
the per-VM waits overlap (wall-clock ~= max, not sum) instead of stacking. A
single VM whose agent never answers still fails loud, naming that VM.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, NativeCommunicator
from testrange.credentials import PosixCred
from testrange.credentials.base import Credential
from testrange.devices import CPU, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.exceptions import GuestAgentError, OrchestratorError
from testrange.guest_io import GuestExec
from testrange.networks import Network, Switch
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.executor import realize_graph
from testrange.state.store import StateStore, run_dir_for
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor

_PROBE_DELAY_S = 0.05


class _SleepyNativeDriver(MockDriver):
    """Native exec sleeps before answering and records peak concurrency.

    ``max_in_flight`` lets a test assert the per-VM readiness waits genuinely
    overlap (>= 2 at once) rather than relying on a flaky wall-clock bound.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def native_guest_execute(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestExec:
        del credential

        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            with self._lock:
                self._in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self._in_flight)
            try:
                time.sleep(_PROBE_DELAY_S)
            finally:
                with self._lock:
                    self._in_flight -= 1
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute


class _OneUnreachableDriver(MockDriver):
    """The VM whose backend name contains ``vm2`` never gets a live agent."""

    def native_guest_execute(
        self, backend_name: str, *, credential: Credential | None = None
    ) -> GuestExec:
        del credential

        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            if "vm2" in backend_name:
                raise GuestAgentError(f"mock: agent down on {backend_name!r}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute


def _native_plan(n_vms: int) -> Plan:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 256))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.1.0/24"))
    for i in range(n_vms):
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name=f"vm{i}",
                    devices=[
                        CPU(1),
                        Memory(256),
                        OSDrive(hyp.pools["pool1"], 8),
                        NetworkIface(hyp.networks["netA"], addr=StaticAddr(f"10.0.1.{100 + i}")),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[PosixCred("u", password="p")],
                ),
                communicator=NativeCommunicator(),
            )
        )
    return Plan("p", hyp)


def _ctx(
    plan: Plan, driver: MockDriver, tmp_path: Path, *, agent_timeout: float = 60.0
) -> GraphContext:
    store = StateStore(run_dir_for("r1", root=tmp_path / "state"))
    store.initialize(run_id="r1", plan_name="p", driver_class="MockDriver", driver_uri="")
    built = tmp_path / "built.qcow2"
    built.write_bytes(b"BUILT-OS-DISK")
    switches = plan.hypervisor.declared_switches
    return GraphContext(
        plan=plan,
        resolved=ResolvedBackend(driver=driver, driver_uri=""),
        store=store,
        cache=CacheManager(local=LocalCache(root=tmp_path / "cache")),
        run_id="r1",
        plan_name="p",
        build_timeout_s=60.0,
        lease_timeout_s=60.0,
        addressing={n.name: NetworkAddressing.from_switch(s) for s in switches for n in s.networks},
        agent_ready_timeout_s=agent_timeout,
        built_disk_paths={vm.name: {"os": built} for vm in plan.hypervisor.declared_vms},
    )


class TestReadinessOverlap:
    def test_communicator_waits_overlap(self, tmp_path: Path) -> None:
        driver = _SleepyNativeDriver(pool_root=tmp_path / "pools")
        plan = _native_plan(4)
        ctx = _ctx(plan, driver, tmp_path)

        realize_graph(ctx, plan.graph)
        # Structural overlap (not wall-clock): at least two readiness probes ran
        # at once on the pool, proving the per-VM waits overlapped within the
        # realize wave.
        assert driver.max_in_flight >= 2, (
            f"expected overlapped readiness waits, peak in-flight was {driver.max_in_flight}"
        )

    def test_one_unreachable_agent_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _s: None)  # skip the 2s retry backoff
        driver = _OneUnreachableDriver(pool_root=tmp_path / "pools")
        plan = _native_plan(4)
        ctx = _ctx(plan, driver, tmp_path, agent_timeout=0.05)

        with pytest.raises(OrchestratorError, match="vm2"):
            realize_graph(ctx, plan.graph)
