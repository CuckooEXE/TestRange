"""Run-phase readiness waits overlap across VMs (ADR-0023, ORCH-19).

The communicator-readiness poll is per-VM and independent; with a thread pool
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
from testrange.devices import CPU, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.exceptions import GuestAgentError, OrchestratorError
from testrange.guest_io import GuestExec
from testrange.networks import Network, Switch
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.run_phase import bind_communicators, wait_communicators_ready
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

    def native_guest_execute(self, backend_name: str) -> GuestExec:
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

    def native_guest_execute(self, backend_name: str) -> GuestExec:
        def _execute(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            if "vm2" in backend_name:
                raise GuestAgentError(f"mock: agent down on {backend_name!r}")
            return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

        return _execute


def _native_plan(n_vms: int) -> Plan:
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
            communicator=NativeCommunicator(),
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


def _ctx(
    plan: Plan, driver: MockDriver, tmp_path: Path, *, agent_timeout: float = 60.0
) -> RunContext:
    store = StateStore(run_dir_for("r1", root=tmp_path / "state"))
    store.initialize(run_id="r1", plan_name="p", driver_class="MockDriver", driver_uri="")
    switches = plan.hypervisor.networks
    return RunContext(
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
    )


class TestReadinessOverlap:
    def test_communicator_waits_overlap(self, tmp_path: Path) -> None:
        driver = _SleepyNativeDriver(pool_root=tmp_path / "pools")
        ctx = _ctx(_native_plan(4), driver, tmp_path)
        bind_communicators(ctx)

        wait_communicators_ready(ctx)
        # Structural overlap (not wall-clock): at least two readiness probes ran
        # at once on the pool, proving the per-VM waits overlapped.
        assert driver.max_in_flight >= 2, (
            f"expected overlapped readiness waits, peak in-flight was {driver.max_in_flight}"
        )

    def test_one_unreachable_agent_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("time.sleep", lambda _s: None)  # skip the 2s retry backoff
        driver = _OneUnreachableDriver(pool_root=tmp_path / "pools")
        ctx = _ctx(_native_plan(4), driver, tmp_path, agent_timeout=0.05)
        bind_communicators(ctx)

        with pytest.raises(OrchestratorError, match="vm2"):
            wait_communicators_ready(ctx)
