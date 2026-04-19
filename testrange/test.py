"""Test definition and execution primitives.

The :class:`Test` class wraps an :class:`~testrange.backends.libvirt.Orchestrator`
configuration and a test function.  Calling :meth:`Test.run` provisions all
VMs, calls the function, and tears everything down — returning a
:class:`TestResult`.

Typical usage::

    from testrange import Test, Orchestrator, VM, VirtualNetwork, vCPU, Memory

    def my_test(orchestrator: Orchestrator) -> None:
        result = orchestrator.vms["server"].exec(["uname", "-r"])
        assert result.exit_code == 0

    test = Test(
        Orchestrator(
            networks=[VirtualNetwork("Net", "10.2.0.0/24", internet=True)],
            vms=[VM("server", "debian-12", users=[...])],
        ),
        my_test,
    )
    result = test.run()
    assert result.passed
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange.backends.libvirt.orchestrator import Orchestrator


@dataclass
class TestResult:
    """The outcome of a single :class:`Test` run.

    :param passed: ``True`` if the test function completed without raising an
        exception, ``False`` otherwise.
    :param error: The exception that caused the failure, or ``None`` on success.
    :param duration: Wall-clock seconds the test run took (including VM
        provisioning time).
    :param traceback_str: Formatted traceback string when *error* is set,
        empty string otherwise.
    """

    passed: bool
    """``True`` if the test function completed without raising an exception."""
    error: BaseException | None
    """The exception that caused the failure, or ``None`` on success."""
    duration: float
    """Wall-clock seconds the test run took (including VM provisioning time)."""
    traceback_str: str = field(default="")
    """Formatted traceback string when :attr:`error` is set, empty string otherwise."""

    def __str__(self) -> str:
        if self.passed:
            return f"PASSED ({self.duration:.1f}s)"
        return (
            f"FAILED ({self.duration:.1f}s): "
            f"{type(self.error).__name__}: {self.error}"
        )


class Test:
    """A self-contained, runnable test definition.

    Combines an :class:`~testrange.backends.libvirt.Orchestrator` configuration
    with a test function.  The test function receives the started orchestrator
    as its only argument (VMs are accessible via :attr:`Orchestrator.vms`).
    It should use ``assert`` statements to signal failures; any unhandled
    exception causes the test to be marked as failed.

    :param orchestrator: A fully configured (but not yet started) orchestrator.
    :param func: The test function to call.  Signature:
        ``def my_test(orchestrator: Orchestrator) -> None: ...``
    :param name: Optional human-readable name for this test.  Defaults to the
        function's ``__name__``.

    Example::

        Test(
            Orchestrator(vms=[VM(...)]),
            my_test_function,
            name="nginx-smoke-test",
        )
    """

    _orchestrator: Orchestrator
    """Configured (but not yet started) orchestrator for this test."""

    _func: Callable[[Orchestrator], None]
    """Test function called with the started orchestrator."""

    name: str
    """Human-readable test name; defaults to the function's ``__name__``."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        func: Callable[[Orchestrator], None],
        name: str | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._func = func
        self.name = name or str(getattr(func, "__name__", repr(func)))

    def run(self) -> TestResult:
        """Provision all VMs, execute the test function, tear down, return result.

        The orchestrator context manager is used to ensure that resources are
        always cleaned up, even if the test function raises.

        :returns: A :class:`TestResult` describing the outcome.
        """
        start = time.monotonic()
        tb_str = ""
        try:
            with self._orchestrator as orch:
                self._func(orch)
            return TestResult(
                passed=True,
                error=None,
                duration=time.monotonic() - start,
            )
        except Exception as exc:
            tb_str = traceback.format_exc()
            return TestResult(
                passed=False,
                error=exc,
                duration=time.monotonic() - start,
                traceback_str=tb_str,
            )


def run_tests(
    tests: list[Test],
    *,
    verbose: bool = True,
    concurrency: int = 1,
) -> list[TestResult]:
    """Run a list of :class:`Test` objects and return their results.

    When *concurrency* is ``1`` (the default) tests run strictly in the
    order given — useful for predictable CI output.  When it's greater
    than one, tests run on a :class:`~concurrent.futures.ThreadPoolExecutor`
    with up to *concurrency* in flight at a time; results are still
    returned in the input order regardless of completion order.

    Concurrency is safe when the tests don't share network topology.
    Each orchestrator opens its own libvirt connection and installs a
    uniquely-named set of objects (``tr-*-<runid>``), so names never
    collide.  Install-phase subnet picking is serialised with a
    file lock (see :mod:`testrange._concurrency`).  User-defined
    :class:`~testrange.backends.libvirt.VirtualNetwork` subnets are
    **not** auto-rewritten — two concurrent tests that both declare
    ``VirtualNetwork("X", "10.0.0.0/24", ...)`` will fail when the
    second tries to bring up the same bridge.  Give each concurrent
    test its own subnet range (or run them with ``concurrency=1``).

    :param tests: Tests to run.
    :param verbose: If ``True``, print a summary line for each test as it
        completes.
    :param concurrency: Maximum number of tests to run at the same time.
        Default ``1`` keeps the original strictly-sequential behaviour.
    :returns: List of :class:`TestResult` objects in the same order as *tests*.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    if concurrency == 1 or len(tests) <= 1:
        return _run_sequential(tests, verbose=verbose)

    return _run_concurrent(tests, verbose=verbose, concurrency=concurrency)


def _run_sequential(tests: list[Test], *, verbose: bool) -> list[TestResult]:
    """Run tests one at a time in the order given."""
    results: list[TestResult] = []
    for test in tests:
        if verbose:
            print(f"  running {test.name!r} ...", end=" ", flush=True)
        result = test.run()
        if verbose:
            print(result)
        results.append(result)

    if verbose:
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"\n{passed}/{total} tests passed.")
    return results


def _run_concurrent(
    tests: list[Test],
    *,
    verbose: bool,
    concurrency: int,
) -> list[TestResult]:
    """Run tests on a ThreadPoolExecutor; preserve input order in results."""
    results: list[TestResult | None] = [None] * len(tests)
    print_lock = threading.Lock()

    def _run_one(index: int) -> tuple[int, TestResult]:
        test = tests[index]
        if verbose:
            with print_lock:
                print(f"  queued {test.name!r} ...", flush=True)
        return index, test.run()

    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="testrange",
    ) as pool:
        futures = [pool.submit(_run_one, i) for i in range(len(tests))]
        for fut in as_completed(futures):
            i, result = fut.result()
            results[i] = result
            if verbose:
                with print_lock:
                    print(f"  {tests[i].name!r} {result}", flush=True)

    assert all(r is not None for r in results)
    final_results: list[TestResult] = [r for r in results if r is not None]

    if verbose:
        passed = sum(1 for r in final_results if r.passed)
        total = len(final_results)
        print(f"\n{passed}/{total} tests passed.")
    return final_results
