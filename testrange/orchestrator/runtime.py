"""Orchestrator runtime.

Drives the lifecycle: preflight -> build -> run -> test -> cleanup. The
Orchestrator brokers between Plan-time data and the driver/cache, respecting
the stovepipe rule — nothing in `testrange.builders`,
`testrange.communicators`, or `testrange.credentials` reaches into the
others. The Orchestrator pulls what each consumer needs from the VMRecipe
and hands it over.
"""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import FrameType, TracebackType
from typing import Any

from testrange._log import get_logger
from testrange.cache.manager import CacheManager
from testrange.drivers import driver_for
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import BuildRequiredError, PreflightError
from testrange.networks.base import NetworkAddressing, Switch
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.build_phase import build_phase, probe_misses
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.run_phase import (
    bind_communicators,
    run_phase,
    wait_builder_ready,
    wait_communicators_ready,
)
from testrange.orchestrator.teardown import teardown
from testrange.plan import Plan
from testrange.state.schema import PHASE_LEAKED
from testrange.state.store import StateStore, new_run_id, run_dir_for
from testrange.vms.handle import VMHandle

_log = get_logger(__name__)


@dataclass(frozen=True)
class OrchestratorHandle:
    """Test-code-facing handle.

    Exposes the run id, the live hypervisor driver, and the per-VM bound
    handles. Test code can reach the driver via ``orch.driver`` for
    backend-level operations not surfaced through a VM's communicator
    (e.g., snapshot, power-state queries).

    ``leak`` is a bound method on the parent :class:`Orchestrator`; call
    it to skip teardown on ``__exit__`` (useful for live debugging and
    for the ``testrange repl`` subcommand).
    """

    run_id: str
    driver: HypervisorDriver
    vms: Mapping[str, VMHandle]
    leak: Callable[[], None]


class Orchestrator:
    """Lifecycle context manager.

    ``with Orchestrator(plan) as orch:`` brings the range up
    (preflight -> build -> run) and tears it down on `__exit__`. Every
    exception path goes through cleanup unless ``leak()`` has been called.
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
        require_cache: bool = False,
    ) -> None:
        self.plan = plan
        self._require_cache = require_cache
        run_id = run_id or new_run_id()
        self.ctx = RunContext(
            plan=plan,
            driver=self._build_driver(),
            store=StateStore(run_dir_for(run_id)),
            cache=cache_manager or CacheManager(),
            run_id=run_id,
            plan_name=plan.name,
            build_timeout_s=build_timeout_s,
            lease_timeout_s=lease_timeout_s,
            sidecar_ready_timeout_s=sidecar_ready_timeout_s,
            addressing={
                n.name: NetworkAddressing.from_switch(s)
                for s in self._all_switches()
                for n in s.networks
            },
        )
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

    def _all_switches(self) -> Sequence[Switch]:
        switches = getattr(self.plan.hypervisor, "networks", None)
        if switches is None:
            return ()
        return tuple(switches)

    def _build_driver(self) -> HypervisorDriver:
        return driver_for(self.plan.hypervisor)

    def _preflight_and_initialize(self) -> None:
        """Run read-only preflight (abort on error) and open the state file."""
        build_switch, _ = resolve_build_switch(getattr(self.plan.hypervisor, "build_switch", None))
        report = self.ctx.driver.preflight(
            self.plan,
            cache_manager=self.ctx.cache,
            build_switch=build_switch,
        )
        if not report:
            raise PreflightError(report.render())
        self.ctx.store.initialize(
            run_id=self.ctx.run_id,
            plan_name=self.ctx.plan_name,
            driver_class=self.ctx.driver.DRIVER_NAME,
            driver_uri=getattr(self.plan.hypervisor, "driver_uri", ""),
        )

    def build(self) -> None:
        """Warm the cache only: preflight + build phase, no run VMs, no tests.

        The ``testrange build`` verb. The build phase tears down its own
        ephemeral infra; on success the backend holds nothing and the state
        file is drained and removed. On failure, in-flight build resources are
        torn down before the error propagates.
        """
        self._install_signal_handlers()
        self.ctx.driver.connect()
        try:
            self._preflight_and_initialize()
            try:
                build_phase(self.ctx)
            except Exception:
                _log.exception("build failed; tearing down")
                teardown(self.ctx)
                raise
            teardown(self.ctx)  # drain bookkeeping; build_phase already destroyed its infra
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
                    misses = probe_misses(self.ctx)
                    if misses:
                        raise BuildRequiredError(
                            f"{len(misses)} VM(s) not in cache: {', '.join(sorted(misses))}; "
                            f"run `testrange build` first (or drop --require-cache)"
                        )
                else:
                    build_phase(self.ctx)  # auto-build any cache miss
                run_phase(self.ctx)
                self._handle = self._build_handle()
                bind_communicators(self.ctx)
                wait_communicators_ready(self.ctx)
                wait_builder_ready(self.ctx)
            except Exception:
                _log.exception("bring-up failed; tearing down")
                teardown(self.ctx)
                raise
            return self._handle
        except Exception:
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
            try:
                signal.signal(sig, prior)
            except (ValueError, OSError):
                pass

    def _build_handle(self) -> OrchestratorHandle:
        vms_map: dict[str, VMHandle] = {
            vm.name: VMHandle(
                name=vm.name,
                backend_name=self.ctx.driver.compose_resource_name(self.ctx.run_id, "vm", vm.name),
                communicator=vm.communicator,
            )
            for vm in self.plan.hypervisor.vms
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
