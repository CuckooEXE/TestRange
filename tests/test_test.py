"""Unit tests for :mod:`testrange.test`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from testrange.test import Test, TestResult, run_tests


class TestTestResultStr:
    def test_passed_format(self) -> None:
        r = TestResult(passed=True, error=None, duration=1.234)
        assert str(r) == "PASSED (1.2s)"

    def test_failed_format(self) -> None:
        err = ValueError("bad")
        r = TestResult(passed=False, error=err, duration=2.0)
        out = str(r)
        assert out.startswith("FAILED (2.0s)")
        assert "ValueError" in out
        assert "bad" in out


class TestTest:
    def test_name_defaults_to_function_name(self) -> None:
        def my_test(o): pass
        t = Test(MagicMock(), my_test)
        assert t.name == "my_test"

    def test_name_override(self) -> None:
        def my_test(o): pass
        t = Test(MagicMock(), my_test, name="custom")
        assert t.name == "custom"


class TestRun:
    def _make_orch(self) -> MagicMock:
        orch = MagicMock()
        # Context manager returns an orch-like with .vms dict
        orch.__enter__.return_value = MagicMock(vms={"web01": MagicMock()})
        orch.__exit__.return_value = None
        return orch

    def test_passing_test(self) -> None:
        orch = self._make_orch()

        def ok(o): pass

        result = Test(orch, ok).run()
        assert result.passed is True
        assert result.error is None
        assert result.duration >= 0
        assert result.traceback_str == ""
        orch.__enter__.assert_called_once()
        orch.__exit__.assert_called_once()

    def test_failing_test_captures_exception(self) -> None:
        orch = self._make_orch()

        def fail(o):
            raise RuntimeError("boom")

        result = Test(orch, fail).run()
        assert result.passed is False
        assert isinstance(result.error, RuntimeError)
        assert "boom" in result.traceback_str
        # Teardown still runs on failure
        orch.__exit__.assert_called_once()

    def test_assertion_error_captured(self) -> None:
        orch = self._make_orch()

        def assertive(o):
            assert 1 == 2, "one is not two"

        result = Test(orch, assertive).run()
        assert result.passed is False
        assert isinstance(result.error, AssertionError)

    def test_func_receives_orchestrator(self) -> None:
        orch = self._make_orch()
        captured: dict = {}

        def inspect(o):
            captured["orch"] = o

        Test(orch, inspect).run()
        assert captured["orch"] is orch.__enter__.return_value


class TestRunTests:
    def _mock_test(self, name: str, passed: bool) -> Test:
        t = MagicMock(spec=Test)
        t.name = name
        t.run.return_value = TestResult(
            passed=passed,
            error=None if passed else RuntimeError("x"),
            duration=0.1,
        )
        return t

    def test_runs_all_tests(self) -> None:
        tests = [self._mock_test(f"t{i}", True) for i in range(3)]
        results = run_tests(tests, verbose=False)
        assert len(results) == 3
        assert all(r.passed for r in results)

    def test_preserves_order(self) -> None:
        tests = [
            self._mock_test("a", True),
            self._mock_test("b", False),
            self._mock_test("c", True),
        ]
        results = run_tests(tests, verbose=False)
        assert [r.passed for r in results] == [True, False, True]

    def test_verbose_prints_summary(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        tests = [self._mock_test("x", True)]
        run_tests(tests, verbose=True)
        out = capsys.readouterr().out
        assert "x" in out
        assert "1/1" in out or "passed" in out

    def test_quiet_no_output(self, capsys: pytest.CaptureFixture) -> None:
        tests = [self._mock_test("x", True)]
        run_tests(tests, verbose=False)
        assert capsys.readouterr().out == ""

    def test_rejects_concurrency_below_one(self) -> None:
        with pytest.raises(ValueError):
            run_tests([], concurrency=0)

    def test_concurrency_one_is_strictly_sequential(self) -> None:
        import threading
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def _slow_run(*_a, **_kw) -> TestResult:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            # tiny sleep to widen any race window
            import time
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            return TestResult(passed=True, error=None, duration=0.0)

        tests = [self._mock_test(f"t{i}", True) for i in range(4)]
        for t in tests:
            t.run.side_effect = _slow_run
        run_tests(tests, verbose=False, concurrency=1)
        assert peak == 1, f"expected serial execution, saw peak={peak}"

    def test_concurrency_gt_one_actually_parallelises(self) -> None:
        import threading
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def _slow_run(*_a, **_kw) -> TestResult:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            import time
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return TestResult(passed=True, error=None, duration=0.0)

        tests = [self._mock_test(f"t{i}", True) for i in range(4)]
        for t in tests:
            t.run.side_effect = _slow_run
        run_tests(tests, verbose=False, concurrency=3)
        assert peak >= 2, (
            f"expected at least 2 tests in flight with concurrency=3, "
            f"saw peak={peak}"
        )

    def test_concurrent_results_returned_in_input_order(self) -> None:
        # Make later tests finish FIRST by varying sleep durations.
        import time
        tests = [self._mock_test(f"t{i}", True) for i in range(5)]
        sleeps = [0.05, 0.01, 0.04, 0.0, 0.02]

        def _run_with_sleep(sleep_for: float):
            def _inner(*_a, **_kw) -> TestResult:
                time.sleep(sleep_for)
                return TestResult(
                    passed=True, error=None, duration=sleep_for
                )
            return _inner

        for t, s in zip(tests, sleeps):
            t.run.side_effect = _run_with_sleep(s)

        results = run_tests(tests, verbose=False, concurrency=5)
        # Completion-order would produce [t3, t1, t4, t2, t0]; we want
        # input order: durations match the sleeps list.
        assert [r.duration for r in results] == sleeps
