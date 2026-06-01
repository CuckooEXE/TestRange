"""Unit tests for the bounded parallel-map helper and per-worker driver pool."""

from __future__ import annotations

import threading
import time

import pytest

from testrange.orchestrator._parallel import (
    DEFAULT_MAX_WORKERS,
    parallel_map,
    resolve_workers,
)


class TestResolveWorkers:
    def test_single_item_is_serial(self) -> None:
        assert resolve_workers(1, None) == 1
        assert resolve_workers(0, None) == 1

    def test_default_cap_applied(self) -> None:
        assert resolve_workers(100, None) == DEFAULT_MAX_WORKERS

    def test_never_exceeds_item_count(self) -> None:
        assert resolve_workers(3, None) == 3
        assert resolve_workers(3, 16) == 3

    def test_explicit_jobs_override(self) -> None:
        assert resolve_workers(100, 4) == 4

    def test_jobs_floored_at_one(self) -> None:
        assert resolve_workers(5, 0) == 1
        assert resolve_workers(5, -3) == 1


class TestParallelMap:
    def test_empty(self) -> None:
        assert parallel_map(lambda x: x, []) == []

    def test_preserves_input_order(self) -> None:
        # Reverse-staggered sleeps: later items finish first, but results must
        # still come back in input order, not completion order.
        def work(i: int) -> int:
            time.sleep((5 - i) * 0.01)
            return i * i

        assert parallel_map(work, range(6), jobs=6) == [0, 1, 4, 9, 16, 25]

    def test_runs_concurrently(self) -> None:
        # Eight 100ms sleeps across 8 workers must overlap: wall-clock well under
        # the 800ms serial sum.
        def work(_: int) -> None:
            time.sleep(0.1)

        start = time.monotonic()
        parallel_map(work, range(8), jobs=8)
        elapsed = time.monotonic() - start
        assert elapsed < 0.4, f"expected overlap, took {elapsed:.3f}s"

    def test_jobs_one_is_serial(self) -> None:
        seen: list[int] = []
        parallel_map(seen.append, range(4), jobs=1)
        assert seen == [0, 1, 2, 3]

    def test_first_exception_propagates_with_type(self) -> None:
        def work(i: int) -> int:
            if i == 2:
                raise ValueError(f"boom {i}")
            return i

        with pytest.raises(ValueError, match="boom 2"):
            parallel_map(work, range(5), jobs=5)

    def test_inflight_workers_drain_before_failure_escapes(self) -> None:
        # The fail-fast contract: an in-flight worker is awaited (not abandoned)
        # before the exception leaves parallel_map, so a sibling that already
        # started mutating shared state finishes — the orchestrator's no-leak
        # guarantee. Worker 0 is slow and records completion; worker 1 raises at
        # once. After parallel_map raises, worker 0 must have finished.
        finished: list[int] = []

        def work(i: int) -> None:
            if i == 1:
                raise ValueError("boom 1")
            time.sleep(0.05)
            finished.append(i)

        with pytest.raises(ValueError, match="boom 1"):
            parallel_map(work, range(2), jobs=2)
        assert finished == [0], "in-flight worker was abandoned instead of joined"

    def test_concurrent_mutation_under_caller_lock_is_consistent(self) -> None:
        # The helper itself does not lock shared state — callers do. With a lock,
        # concurrent appends from every worker land without loss.
        lock = threading.Lock()
        bag: list[int] = []

        def work(i: int) -> None:
            time.sleep(0.005)
            with lock:
                bag.append(i)

        parallel_map(work, range(50), jobs=8)
        assert sorted(bag) == list(range(50))
