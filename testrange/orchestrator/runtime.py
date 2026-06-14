"""Orchestrator runtime.

Drives the lifecycle over the plan's frozen build graph (ADR-0030): preflight
-> materialize walk (build) -> realize walk (run) -> tests -> teardown. The
Orchestrator brokers between Plan-time data and the driver/cache, respecting
the stovepipe rule — nothing in ``testrange.builders``,
``testrange.communicators``, or ``testrange.credentials`` reaches into the
others. Node hooks pull what each consumer needs from the
:class:`~testrange.orchestrator.context.GraphContext` the Orchestrator
assembles here.
"""

from __future__ import annotations

import contextlib
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.cache.manager import CacheManager
from testrange.connect import BackendProfile
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import BuildRequiredError, PreflightError, StateError
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import (
    ResolvedBackend,
    compatibility_findings,
    resolve_backend,
)
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.dashboard_state import DashboardState
from testrange.orchestrator.executor import (
    materialize_graph,
    probe_misses,
    realize_graph,
)
from testrange.orchestrator.teardown import teardown
from testrange.preflight import PreflightReport
from testrange.state.schema import PHASE_LEAKED
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms.handle import RunningVM

if TYPE_CHECKING:  # pragma: no cover
    from testrange.plan import Plan

_log = get_logger(__name__)


@dataclass(frozen=True)
class OrchestratorHandle:
    """Test-code-facing handle.

    Exposes the run id, the live hypervisor driver, and the per-VM bound
    views. Test code can reach the driver via ``orch.driver`` for
    backend-level operations not surfaced through a VM's communicator
    (e.g., snapshot, power-state queries).

    ``leak`` is a bound method on the parent :class:`Orchestrator`; call
    it to skip teardown on ``__exit__`` (useful for live debugging and
    for the ``testrange repl`` subcommand).
    """

    run_id: str
    driver: HypervisorDriver
    vms: Mapping[str, RunningVM]
    leak: Callable[[], None]


class Orchestrator:
    """Lifecycle context manager.

    ``with Orchestrator(plan) as orch:`` brings the range up
    (preflight -> materialize -> realize) and tears it down on ``__exit__``.
    Every exception path goes through cleanup unless ``leak()`` has been
    called.
    """

    def __init__(
        self,
        plan: Plan,
        *,
        cache_manager: CacheManager | None = None,
        run_id: str | None = None,
        build_timeout_s: float = 600.0,
        lease_timeout_s: float = 120.0,
        sidecar_ready_timeout_s: float = 120.0,
        agent_ready_timeout_s: float = 120.0,
        require_cache: bool = False,
        profile: BackendProfile | None = None,
        jobs: int | None = None,
        dashboard: DashboardState | None = None,
        resume: bool = False,
    ) -> None:
        self.plan = plan
        self._require_cache = require_cache
        if resume and run_id is None:
            raise ValueError("resume=True requires the run_id of the run to resume")
        # Fold the Plan entry + optional connection profile into the single
        # backend binding (CORE-10). A pin/driver mismatch or a backend-agnostic
        # plan with no profile raises here, at construction.
        self._resolved: ResolvedBackend = resolve_backend(plan, profile)
        run_id = run_id or new_run_id()
        self.ctx = GraphContext(
            plan=plan,
            resolved=self._resolved,
            store=StateStore(run_dir_for(run_id)),
            cache=cache_manager or CacheManager(),
            run_id=run_id,
            plan_name=plan.name,
            build_timeout_s=build_timeout_s,
            lease_timeout_s=lease_timeout_s,
            sidecar_ready_timeout_s=sidecar_ready_timeout_s,
            agent_ready_timeout_s=agent_ready_timeout_s,
            addressing={
                n.name: NetworkAddressing.from_switch(s)
                for s in plan.hypervisor.declared_switches
                for n in s.networks
            },
            jobs=jobs,
            resume=resume,
            # The CLI owns the dashboard so it can render the same state the
            # node hooks write; a library call with no dashboard gets a fresh one.
            dashboard=dashboard if dashboard is not None else DashboardState(),
        )
        # Register every run VM up front so the dashboard shows them PENDING
        # before bring-up touches them (in plan order).
        self.ctx.dashboard.seed_vms(vm.name for vm in plan.hypervisor.declared_vms)
        self._handle: OrchestratorHandle | None = None
        self._leak = False

    @property
    def run_id(self) -> str:
        return self.ctx.run_id

    @property
    def driver(self) -> HypervisorDriver:
        return self.ctx.driver

    @property
    def cache(self) -> CacheManager:
        return self.ctx.cache

    @property
    def build_timeout_s(self) -> float:
        return self.ctx.build_timeout_s

    @property
    def lease_timeout_s(self) -> float:
        return self.ctx.lease_timeout_s

    @property
    def agent_ready_timeout_s(self) -> float:
        return self.ctx.agent_ready_timeout_s

    def _preflight_and_initialize(self) -> None:
        """Run read-only preflight (abort on error) and open the state file."""
        # A cache-only run (require_cache) never builds, so it never realizes the
        # build switch — pass None so preflight skips its live checks (CORE-65).
        build_switch = (
            None if self._require_cache else resolve_build_switch(self.plan.hypervisor.build_switch)
        )
        report = self.ctx.driver.preflight(
            self.plan,
            cache_manager=self.ctx.cache,
            build_switch=build_switch,
        )
        # Merge the portability-lint layer (CORE-10 layer 2) with the driver's
        # own live findings (layer 3); pin/driver-match (layer 1) already ran in
        # resolve_backend at construction. The driver's preflight owns the
        # uplink-resolution check (it holds the profile's [uplinks] map and sees
        # the build switch passed above) — ADR-0016.
        report = report.merged(
            PreflightReport(findings=compatibility_findings(self.plan, self.ctx.driver))
        )
        if not report:
            raise PreflightError(report.render())
        if self.ctx.resume:
            state = self.ctx.store.reopen()
            if state.plan_name != self.ctx.plan_name:
                raise StateError(
                    f"run {self.ctx.run_id} was created from plan {state.plan_name!r}; "
                    f"refusing to resume it with plan {self.ctx.plan_name!r}"
                )
            # Seed the in-memory completion mirror from the reopened ledger so
            # node hooks can skip / reattach what already finished (DAG-9).
            for record in state.nodes:
                if record.materialized_at is not None:
                    self.ctx.materialized_nodes.add(record.name)
                if record.realized_at is not None:
                    self.ctx.realized_nodes.add(record.name)
            _log.info(
                "resume %s: %d node(s) materialized, %d realized",
                self.ctx.run_id,
                len(self.ctx.materialized_nodes),
                len(self.ctx.realized_nodes),
            )
        else:
            self.ctx.store.initialize(
                run_id=self.ctx.run_id,
                plan_name=self.ctx.plan_name,
                driver_class=self.ctx.driver.DRIVER_NAME,
                driver_uri=self._resolved.driver_uri,
            )

    def build(self) -> None:
        """Warm the cache only: preflight + materialize walk, no run VMs, no tests.

        The ``testrange build`` verb. The materialize walk tears down its own
        ephemeral infra; on success the backend holds nothing and the state
        file is drained and removed. On failure, in-flight build resources are
        torn down before the error propagates.
        """
        self._install_signal_handlers()
        self.ctx.driver.connect()
        try:
            self._preflight_and_initialize()
            try:
                materialize_graph(self.ctx, self.plan.graph)
            except Exception:
                _log.exception("build failed; tearing down")
                teardown(self.ctx)
                raise
            teardown(self.ctx)  # drain bookkeeping; the walk already destroyed its infra
        finally:
            self._restore_signal_handlers()
            self.ctx.driver.disconnect()

    def __enter__(self) -> OrchestratorHandle:
        self._install_signal_handlers()
        self.ctx.driver.connect()
        try:
            self._preflight_and_initialize()
            try:
                if self._require_cache:
                    # Verify the cache instead of building: a miss fails fast so
                    # CI keeps build and run as distinct invocations (ADR-0010 §1).
                    misses = probe_misses(self.ctx, self.plan.graph)
                    if misses:
                        raise BuildRequiredError(
                            f"{len(misses)} VM(s) not in cache: {', '.join(misses)}; "
                            f"run `testrange build` first (or drop --require-cache)"
                        )
                    materialize_graph(self.ctx, self.plan.graph)  # ledger the hits
                else:
                    materialize_graph(self.ctx, self.plan.graph)  # auto-build any miss
                realize_graph(self.ctx, self.plan.graph)
                self._handle = self._build_handle()
            except BaseException:
                # BaseException, not Exception: a Ctrl-C / SIGTERM lands as
                # KeyboardInterrupt (BaseException), and it fires inside __enter__
                # — so Python never calls __exit__ (where the teardown lives). If
                # this handler only caught Exception the interrupt would slip past
                # and leak every resource the walks created. We tear down and
                # re-raise (never swallow), so the operator still sees the
                # interrupt.
                _log.exception("bring-up failed or interrupted; tearing down")
                # parallel_map is fail-fast: the worker that raised tagged its
                # own VM FAILED; sweep any sibling left mid-stage so the final
                # dashboard frame is truthful rather than frozen at e.g. booting.
                self.ctx.dashboard.abort_unfinished()
                teardown(self.ctx)
                raise
            return self._handle
        except BaseException:
            # Same rationale: an interrupt during preflight/initialize (before the
            # inner try) or the inner handler's re-raise must still release the
            # driver connection rather than leak it. Because __enter__ raised,
            # Python never calls __exit__, so this is the only place the signal
            # handlers installed above get restored — mirror build()'s finally so
            # a failed entry (e.g. a routine PreflightError) does not leave
            # SIGTERM/SIGHUP rewired to our handler for the rest of the process.
            self._restore_signal_handlers()
            self.ctx.driver.disconnect()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_val, exc_tb
        try:
            if self._leak:
                _log.warning("leak: skipping teardown; run state retained")
                self.ctx.store.set_phase(PHASE_LEAKED)
                self.ctx.store.release()
            else:
                if exc_type is not None:
                    _log.info("tearing down after %s", exc_type.__name__)
                teardown(self.ctx)
        finally:
            self._restore_signal_handlers()
            self.ctx.driver.disconnect()

    def leak(self) -> None:
        """Skip teardown on ``__exit__``. Use for live debugging."""
        self._leak = True

    def _install_signal_handlers(self) -> None:
        """Route SIGTERM/SIGHUP through ``__exit__``'s cleanup path.

        The handler raises ``KeyboardInterrupt`` so an in-flight bring-up
        unwinds into teardown. Limitation: Python delivers the exception into
        whatever bytecode is executing at signal time. During bring-up that is
        our own code and unwinds cleanly; but if a signal lands *mid-test*
        while control is inside a Communicator's blocking I/O (paramiko read
        loops, socket waits), that library may swallow the ``KeyboardInterrupt``
        or be left mid-protocol. A polled ``signal_received`` flag checked at
        safe points would be more robust but is a larger refactor; until then,
        ``kill -9`` plus state-driven ``testrange cleanup`` is the recovery
        path for a wedged mid-test interrupt.
        """
        self._prior_signal_handlers: dict[int, Any] = {}

        def _handler(signum: int, _frame: FrameType | None) -> None:
            _log.warning("received signal %d; raising KeyboardInterrupt for cleanup", signum)
            raise KeyboardInterrupt(f"signal {signum}")

        sigs: tuple[int, ...] = (signal.SIGTERM,)
        if sys.platform != "win32":
            sigs += (signal.SIGHUP,)
        for sig in sigs:
            try:
                self._prior_signal_handlers[sig] = signal.signal(sig, _handler)
            except (ValueError, OSError) as e:
                _log.debug("could not install handler for signal %d: %s", sig, e)

    def _restore_signal_handlers(self) -> None:
        for sig, prior in getattr(self, "_prior_signal_handlers", {}).items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, prior)

    def _build_handle(self) -> OrchestratorHandle:
        vms_map: dict[str, RunningVM] = {
            vm.name: RunningVM(
                name=vm.name,
                backend_name=self.ctx.driver.compose_resource_name(self.ctx.run_id, "vm", vm.name),
                communicator=vm.communicator,
            )
            for vm in self.plan.hypervisor.declared_vms
        }
        return OrchestratorHandle(
            run_id=self.ctx.run_id,
            driver=self.ctx.driver,
            vms=vms_map,
            leak=self.leak,
        )


__all__ = [
    "Orchestrator",
    "OrchestratorHandle",
]
