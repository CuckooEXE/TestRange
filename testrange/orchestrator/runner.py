"""Test runner: bring a range up, execute tests against it, tear it down."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass

from testrange._log import get_logger
from testrange._tui import capture_test_output
from testrange.cache.manager import CacheManager
from testrange.connect import BackendProfile
from testrange.orchestrator.dashboard_state import DashboardState
from testrange.orchestrator.runtime import Orchestrator, OrchestratorHandle
from testrange.plan import Plan

_log = get_logger(__name__)


@dataclass(frozen=True)
class TestResult:
    """Outcome of one test function."""

    name: str
    passed: bool
    error: str | None = None
    duration: float = 0.0

    def report_line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        line = f"[{status}] {self.name} ({self.duration:.2f}s)"
        if self.error:
            line += f"\n      {self.error}"
        return line


def build_range(
    plan: Plan,
    *,
    cache_manager: CacheManager | None = None,
    profile: BackendProfile | None = None,
    jobs: int | None = None,
    build_timeout_s: float = 600.0,
    dashboard: DashboardState | None = None,
) -> str:
    """Warm the cache for ``plan`` (``testrange build``); run no tests.

    Runs preflight + the build phase only, tearing down all build infra. The
    backend holds nothing afterward. Returns the run id (for logging).
    ``profile`` binds a backend-agnostic plan to a backend (CORE-10/-11).
    ``jobs`` caps the build phase's worker pool (ADR-0023). ``build_timeout_s``
    bounds how long a build VM may take to report its result over serial (default
    600s); slow installer-origin builds — notably a nested ESXi guest install
    under nested KVM — need a larger value.
    """
    o = Orchestrator(
        plan,
        cache_manager=cache_manager,
        profile=profile,
        jobs=jobs,
        build_timeout_s=build_timeout_s,
        dashboard=dashboard,
    )
    o.build()
    return o.run_id


def run_tests(
    tests: list[Callable[[OrchestratorHandle], None]],
    plan: Plan,
    *,
    cache_manager: CacheManager | None = None,
    fail_fast: bool = False,
    leak_on_failure: bool = False,
    require_cache: bool = False,
    profile: BackendProfile | None = None,
    verbose: bool = False,
    jobs: int | None = None,
    build_timeout_s: float = 600.0,
    lease_timeout_s: float = 120.0,
    ready_timeout_s: float = 120.0,
    dashboard: DashboardState | None = None,
) -> list[TestResult]:
    """Bring the range up, execute the tests, tear it down.

    Auto-builds any cache miss before running (so a cold cache just works);
    ``require_cache=True`` instead fails fast on a miss without building.
    Tests run sequentially. Continue-on-failure is the default;
    ``fail_fast=True`` stops on the first failure. With
    ``leak_on_failure=True``, if any test fails the orchestrator skips
    teardown and the user can SSH in to debug; tear down later with
    ``testrange cleanup <run_id>``.

    ``build_timeout_s`` bounds how long a build VM may take to report its result
    over serial (default 600s). ``lease_timeout_s`` bounds how long a run VM may
    take to acquire its DHCP lease (default 120s). ``ready_timeout_s`` bounds how
    long a bound communicator may take to answer its first probe (default 120s).
    Slow guests — notably a nested ESXi node, whose install is slow under nested
    KVM and whose sshd only comes up via ``local.sh`` after hostd, minutes past
    its DHCP lease — need larger values.
    """
    results: list[TestResult] = []
    o = Orchestrator(
        plan,
        cache_manager=cache_manager,
        require_cache=require_cache,
        profile=profile,
        jobs=jobs,
        build_timeout_s=build_timeout_s,
        lease_timeout_s=lease_timeout_s,
        agent_ready_timeout_s=ready_timeout_s,
        dashboard=dashboard,
    )
    with o as orch:
        _execute_tests(
            orch, tests, results, fail_fast=fail_fast, verbose=verbose, dashboard=o.ctx.dashboard
        )
        if leak_on_failure and any(not r.passed for r in results):
            _log.warning("--leak-on-failure: skipping teardown; run_id=%s", o.run_id)
            o.leak()
    return results


def _execute_tests(
    orch: OrchestratorHandle,
    tests: list[Callable[[OrchestratorHandle], None]],
    results: list[TestResult],
    *,
    fail_fast: bool,
    verbose: bool = False,
    dashboard: DashboardState | None = None,
) -> None:
    """Run tests sequentially, capture failures, append to ``results``.

    Under ``verbose`` each test's ``stdout``/``stderr`` is teed into the live
    tail (CORE-6); otherwise prints pass straight through as before. Each test's
    running/pass/fail state is mirrored into ``dashboard`` for the live view.
    """
    for t in tests:
        name = getattr(t, "__name__", repr(t))
        if dashboard is not None:
            dashboard.start_test(name)
        start = time.monotonic()
        try:
            with capture_test_output(name) if verbose else nullcontext():
                t(orch)
        except Exception as e:
            tb = traceback.format_exc()
            duration = time.monotonic() - start
            error = tb if tb.strip() else str(e)
            results.append(TestResult(name=name, passed=False, error=error, duration=duration))
            if dashboard is not None:
                dashboard.finish_test(name, passed=False, duration=duration, error=error)
            if fail_fast:
                _log.warning("--fail-fast: stopping on %s", name)
                return
            continue
        duration = time.monotonic() - start
        results.append(TestResult(name=name, passed=True, duration=duration))
        if dashboard is not None:
            dashboard.finish_test(name, passed=True, duration=duration)


__all__ = ["TestResult", "build_range", "run_tests"]
