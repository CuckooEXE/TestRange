"""Tests for the test-output tee (CORE-6, ADR-0029).

The collapsing live-tail renderer was retired with the rich migration (CORE-78);
what remains here is :func:`capture_test_output`, which teas a test's
``stdout``/``stderr`` into the ``TESTOUT_LOGGER`` firehose, scrubbing control
bytes and surviving a re-entrant failing handler.
"""

from __future__ import annotations

import io
import logging

import pytest

from testrange._tui import TESTOUT_LOGGER, capture_test_output


class _Records(logging.Handler):
    """Collect emitted records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_capture_tees_lines_scrubbed_and_step_tagged() -> None:
    logger = logging.getLogger(TESTOUT_LOGGER)
    sink = _Records()
    logger.addHandler(sink)
    prev = logger.level
    logger.setLevel(logging.INFO)
    try:
        with capture_test_output("test_login"):
            print("hello \x1b[31mred\x1b[0m world")  # noqa: T201 — control bytes scrubbed
    finally:
        logger.removeHandler(sink)
        logger.setLevel(prev)
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.getMessage() == "hello red world"  # escapes stripped
    assert rec.tr_step == "test_login"  # type: ignore[attr-defined]


def test_capture_flushes_trailing_partial_line() -> None:
    logger = logging.getLogger(TESTOUT_LOGGER)
    sink = _Records()
    logger.addHandler(sink)
    prev = logger.level
    logger.setLevel(logging.INFO)
    try:
        with capture_test_output("test_x"):
            print("no newline", end="")  # noqa: T201 — flushed on context exit
    finally:
        logger.removeHandler(sink)
        logger.setLevel(prev)
    assert [r.getMessage() for r in sink.records] == ["no newline"]


def test_partial_line_flush_with_richhandler_on_tree_does_not_recurse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a no-newline print, flushed while a RichHandler sits on the
    ``testrange`` tree, must not infinite-loop.

    The RichHandler renders the teed record to the *redirected* stderr and calls
    flush() mid-emit; without a re-entrancy guard on ``_LineLogWriter.flush`` the
    still-buffered partial line is re-emitted forever (the hang seen running the
    suite with ``-v``). This is also reachable in a real ``run --verbose``.
    """
    from rich.logging import RichHandler

    from testrange._console import err_console

    tree = logging.getLogger("testrange")
    testout = logging.getLogger(TESTOUT_LOGGER)
    handler = RichHandler(console=err_console(), markup=False)  # writes to live sys.stderr
    handler.setLevel(logging.DEBUG)
    tree.addHandler(handler)
    saved = (testout.level, testout.propagate)
    testout.setLevel(logging.INFO)
    testout.propagate = True
    monkeypatch.setattr(
        "sys.__stderr__", io.StringIO()
    )  # keep the re-entrant write off real stderr
    try:
        with capture_test_output("test_x"):
            print("partial line, no newline", end="")  # noqa: T201
        # Reached here → no RecursionError, no hang.
    finally:
        tree.removeHandler(handler)
        testout.setLevel(saved[0])
        testout.propagate = saved[1]


def test_handler_on_closed_stream_does_not_recurse(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reproduces the real footgun: a StreamHandler whose stream is closed (a
    # stale handler left over from an earlier run). Its emit hits the closed
    # file, logging.handleError writes the traceback to sys.stderr — which
    # capture_test_output has redirected into the same writer. Without a
    # re-entrancy guard the writer logs again -> closed file -> handleError ->
    # ... RecursionError. The guard must break the cycle (CORE-6).
    closed = io.StringIO()
    closed.close()
    logger = logging.getLogger(TESTOUT_LOGGER)
    handler = logging.StreamHandler(closed)
    logger.addHandler(handler)
    prev_level = logger.level
    logger.setLevel(logging.INFO)
    real_err = io.StringIO()
    monkeypatch.setattr("sys.__stderr__", real_err)
    try:
        with capture_test_output("test_x"):
            print("line that triggers a failing emit")  # noqa: T201
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
    # We got here without a RecursionError; the guard passed the re-entrant
    # traceback write through to the real stderr.
    assert "I/O operation on closed file" in real_err.getvalue()
