"""Tests for the collapsing live-tail renderer (CORE-6)."""

from __future__ import annotations

import io
import logging

from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER, LiveTail, capture_test_output


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _console_rec(vm: str, text: str) -> logging.LogRecord:
    """A record shaped like ``_ConsoleStreamer``'s ``_console.debug("[%s] %s", vm, text)``."""
    return logging.LogRecord(
        CONSOLE_LOGGER, logging.DEBUG, __file__, 0, "[%s] %s", (vm, text), None
    )


def _testout_rec(test: str, text: str) -> logging.LogRecord:
    rec = logging.LogRecord(TESTOUT_LOGGER, logging.INFO, __file__, 0, "%s", (text,), None)
    rec.tr_step = test
    return rec


def _progress_rec(msg: str) -> logging.LogRecord:
    """A permanent (non-transient) progress record from the orchestrator."""
    return logging.LogRecord(
        "testrange.orchestrator.build_phase", logging.INFO, __file__, 0, msg, (), None
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TestNonTTY:
    def test_plain_per_line_no_escapes(self) -> None:
        out = io.StringIO()  # not a tty
        tail = LiveTail(out, tty=False)
        tail.emit(_console_rec("web", "line one"))
        tail.emit(_console_rec("web", "line two"))
        text = out.getvalue()
        assert "\x1b" not in text  # no ANSI on a non-tty
        assert text == "[web] line one\n[web] line two\n"


class TestTTYRegion:
    def test_region_keeps_only_last_height_lines(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True)
        for i in range(6):
            tail.emit(_console_rec("web", f"line {i}"))
        assert list(tail._ring) == ["[web] line 3", "[web] line 4", "[web] line 5"]

    def test_redraw_moves_cursor_up_by_drawn_lines(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=5, width=80, tty=True)
        tail.emit(_console_rec("web", "first"))
        tail.emit(_console_rec("web", "second"))
        text = out.getvalue()
        assert "\x1b[1A" in text  # rewind one line before the second redraw
        assert "first" in text and "second" in text

    def test_long_line_truncated_to_width(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=10, tty=True)
        tail.emit(_console_rec("web", "x" * 40))
        for seg in out.getvalue().split("\x1b[K")[:-1]:
            visible = seg.rsplit("\n", 1)[-1].lstrip("\r")
            assert len(visible) <= 10


class TestPermanentCommit:
    def test_non_transient_record_is_committed_above_region(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True)
        tail.emit(_console_rec("web", "build chatter"))
        tail.emit(_progress_rec("vm web: build reported success"))
        text = out.getvalue()
        assert "vm web: build reported success" in text
        # The committed line precedes the redraw of the still-live region.
        assert text.rindex("build chatter") > text.rindex("vm web: build reported success")


class TestStepCollapse:
    def test_step_change_collapses_previous_with_elapsed(self) -> None:
        clock = _Clock()
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True, now=clock)
        tail.emit(_console_rec("web", "installing"))
        clock.t = 47.0
        tail.emit(_console_rec("db", "installing"))  # new step -> collapse web
        text = out.getvalue()
        assert "=> build web" in text
        assert "DONE" in text and "47s" in text

    def test_explicit_step_via_tr_step_attr(self) -> None:
        clock = _Clock()
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True, now=clock)
        tail.emit(_testout_rec("test_login", "print output"))
        clock.t = 3.0
        tail.finish(ok=True)
        text = out.getvalue()
        assert "=> test_login" in text and "DONE" in text and "3s" in text


class TestTeardown:
    def test_finish_failure_marks_step_failed(self) -> None:
        clock = _Clock()
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True, now=clock)
        tail.emit(_console_rec("web", "boom"))
        clock.t = 5.0
        tail.finish(ok=False)
        assert "FAIL" in out.getvalue()

    def test_finish_restores_cursor_and_clears_region(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=3, width=80, tty=True)
        tail.emit(_console_rec("web", "line"))
        tail.finish(ok=True)
        assert tail._drawn == 0
        assert out.getvalue().endswith("\x1b[?25h")  # cursor shown; terminal not wedged


class TestCaptureReentrancy:
    def test_handler_on_closed_stream_does_not_recurse(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Reproduces the real footgun: a StreamHandler whose stream is closed
        # (a stale handler left over from an earlier run). Its emit hits the
        # closed file, logging.handleError writes the traceback to sys.stderr —
        # which capture_test_output has redirected into the same writer. Without
        # a re-entrancy guard the writer logs again -> closed file -> handleError
        # -> ... RecursionError. The guard must break the cycle (CORE-6).
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


class TestResize:
    def test_resize_shrinks_ring(self) -> None:
        out = _FakeTTY()
        tail = LiveTail(out, height=10, width=80, tty=True)
        for i in range(8):
            tail.emit(_console_rec("web", f"l{i}"))
        tail.resize(width=40, height=3)
        assert tail._ring.maxlen == 3
        assert list(tail._ring) == ["[web] l5", "[web] l6", "[web] l7"]
        assert tail._width == 40
