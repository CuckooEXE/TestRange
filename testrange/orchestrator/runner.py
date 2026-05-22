"""Test runner: bring a range up, execute tests against it, tear it down."""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from testrange._log import get_logger
from testrange.cache.manager import CacheManager
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


def build_range(plan: Plan, *, cache_manager: CacheManager | None = None) -> str:
    """Warm the cache for ``plan`` (``testrange build``); run no tests.

    Runs preflight + the build phase only, tearing down all build infra. The
    backend holds nothing afterward. Returns the run id (for logging).
    """
    o = Orchestrator(plan, cache_manager=cache_manager)
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
) -> list[TestResult]:
    """Bring the range up, execute the tests, tear it down.

    Auto-builds any cache miss before running (so a cold cache just works);
    ``require_cache=True`` instead fails fast on a miss without building.
    Tests run sequentially. Continue-on-failure is the default;
    ``fail_fast=True`` stops on the first failure. With
    ``leak_on_failure=True``, if any test fails the orchestrator skips
    teardown and the user can SSH in to debug; tear down later with
    ``testrange cleanup <run_id>``.
    """
    results: list[TestResult] = []
    o = Orchestrator(plan, cache_manager=cache_manager, require_cache=require_cache)
    with o as orch:
        _execute_tests(orch, tests, results, fail_fast=fail_fast)
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
) -> None:
    """Run tests sequentially, capture failures, append to ``results``."""
    for t in tests:
        name = getattr(t, "__name__", repr(t))
        start = time.monotonic()
        try:
            t(orch)
        except Exception as e:
            tb = traceback.format_exc()
            results.append(
                TestResult(
                    name=name,
                    passed=False,
                    error=tb if tb.strip() else str(e),
                    duration=time.monotonic() - start,
                )
            )
            if fail_fast:
                _log.warning("--fail-fast: stopping on %s", name)
                return
            continue
        results.append(TestResult(name=name, passed=True, duration=time.monotonic() - start))


__all__ = ["TestResult", "build_range", "run_tests"]
