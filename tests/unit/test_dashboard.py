"""Unit tests for the rich dashboard renderer + log handler (ADR-0029).

Rendering is captured against a force-terminal Console writing to a buffer, so no
real TTY is needed; the handler is driven with synthetic LogRecords shaped like
the real firehose.
"""

from __future__ import annotations

import io
import logging

import pytest
from rich.console import Console

from testrange._dashboard import DashboardLogHandler, render, run_dashboard
from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER
from testrange.orchestrator.dashboard_state import DashboardState, VMStage


def _capture(renderable: object, *, width: int = 120, height: int = 40) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=width, height=height)
    console.print(renderable)
    return buf.getvalue()


def _populated() -> DashboardState:
    s = DashboardState()
    s.seed_vms(["web", "db"])
    s.set_vm_stage("web", VMStage.READY)
    s.set_vm_stage("db", VMStage.FAILED, detail="disk upload failed")
    s.start_test("login_works")
    s.finish_test("login_works", passed=True, duration=0.42)
    s.finish_test("logout_works", passed=False, duration=1.1, error="AssertionError")
    s.append_log("orchestrator: run phase complete")
    s.append_serial("web", "Debian GNU/Linux 13 web ttyS0")
    return s


def test_render_shows_vm_names_stages_tests_and_streams() -> None:
    out = _capture(render(_populated().snapshot()))
    # panes present
    for title in ("VMs", "Tests", "Log", "Serial"):
        assert title in out
    # vm + stage
    assert "web" in out and "db" in out
    assert "ready" in out and "failed" in out
    # the failure detail rides next to the VM
    assert "disk upload failed" in out
    # tests + a serial line + a log line
    assert "login_works" in out and "logout_works" in out
    assert "run phase complete" in out
    assert "Debian GNU/Linux 13" in out


def test_render_does_not_interpret_guest_markup() -> None:
    """A serial line containing rich-markup syntax must render literally."""
    s = DashboardState()
    s.append_serial("web", "boot stage [red]ERROR[/red] not markup")
    out = _capture(render(s.snapshot()))
    assert "[red]ERROR[/red]" in out  # shown verbatim, not parsed away


def test_render_tail_keeps_most_recent_lines() -> None:
    """A short serial pane shows the latest lines, not the oldest."""
    s = DashboardState()
    for i in range(200):
        s.append_serial("web", f"serial line {i}")
    out = _capture(render(s.snapshot()), height=24)
    assert "serial line 199" in out  # newest visible
    assert "serial line 0" not in out  # oldest scrolled off


def _console_record(vm: str, line: str) -> logging.LogRecord:
    # Mirrors _ConsoleStreamer: _console.debug("[%s] %s", vm_name, line).
    return logging.LogRecord(
        CONSOLE_LOGGER, logging.DEBUG, __file__, 0, "[%s] %s", (vm, line), None
    )


def test_handler_routes_console_records_to_serial_ring() -> None:
    s = DashboardState()
    h = DashboardLogHandler(s)
    h.emit(_console_record("web", "kernel: booting"))
    assert s.snapshot().serial_lines == (("web", "kernel: booting"),)
    assert s.snapshot().log_lines == ()  # not duplicated into the log pane


def test_handler_routes_other_records_to_log_ring_with_richhandler_fields() -> None:
    s = DashboardState()
    h = DashboardLogHandler(s)
    rec = logging.LogRecord(
        "testrange.orchestrator.run_phase", logging.INFO, __file__, 0, "sidecar ready", None, None
    )
    rec.run_id = "r-42"  # injected by _RunIdAdapter in real use
    h.emit(rec)
    # LEVEL [run_id] short-name: message — the same fields RichHandler shows.
    assert s.snapshot().log_lines == ("INFO [r-42] run_phase: sidecar ready",)
    assert s.snapshot().serial_lines == ()


def test_log_pane_colourises_the_level() -> None:
    """The Log pane renders the leading level token in its severity colour."""
    s = DashboardState()
    h = DashboardLogHandler(s)
    for level, msg in ((logging.WARNING, "slow upload"), (logging.INFO, "vm ready")):
        rec = logging.LogRecord(
            "testrange.orchestrator.run_phase", level, __file__, 0, msg, None, None
        )
        h.emit(rec)
    out = _capture(render(s.snapshot()))
    # Both levels + their messages are present, and an ANSI SGR colour was emitted
    # for the level tokens (force_terminal Console).
    assert "WARNING" in out and "slow upload" in out
    assert "INFO" in out and "vm ready" in out
    assert "\x1b[" in out  # styled output, not plain


def test_handler_drops_testout_records() -> None:
    s = DashboardState()
    h = DashboardLogHandler(s)
    rec = logging.LogRecord(TESTOUT_LOGGER, logging.INFO, __file__, 0, "stdout chatter", None, None)
    h.emit(rec)
    assert s.snapshot().log_lines == ()
    assert s.snapshot().serial_lines == ()


def test_run_dashboard_inactive_off_tty_yields_none() -> None:
    """A non-terminal console keeps the dashboard off; logging is untouched."""
    s = DashboardState()
    console = Console(file=io.StringIO())  # not a terminal
    cl = logging.getLogger(CONSOLE_LOGGER)
    cl.setLevel(logging.WARNING)
    with run_dashboard(s, enabled=True, console=console) as handle:
        assert handle is None
        assert cl.level == logging.WARNING  # firehose untouched without --verbose
    assert cl.level == logging.WARNING


def test_run_dashboard_verbose_lowers_then_restores_firehose_off_tty() -> None:
    s = DashboardState()
    console = Console(file=io.StringIO())
    cl = logging.getLogger(CONSOLE_LOGGER)
    cl.setLevel(logging.WARNING)
    with run_dashboard(s, enabled=True, console=console, verbose=True):
        assert cl.level == logging.DEBUG  # visible as plain logs in a piped run
    assert cl.level == logging.WARNING  # restored


def test_run_dashboard_active_swaps_handler_and_restores() -> None:
    """On a TTY the dashboard owns the logger for its duration, then restores it."""
    s = DashboardState()
    console = Console(file=io.StringIO(), force_terminal=True, width=80, height=24)
    root = logging.getLogger("testrange")
    cl = logging.getLogger(CONSOLE_LOGGER)
    saved_handlers, saved_prop, saved_cl = root.handlers[:], root.propagate, cl.level
    sentinel = logging.NullHandler()
    root.handlers[:] = [sentinel]
    cl.setLevel(logging.WARNING)
    try:
        with run_dashboard(s, enabled=True, console=console) as handle:
            assert handle is s
            assert any(isinstance(h, DashboardLogHandler) for h in root.handlers)
            assert sentinel not in root.handlers  # original removed so it can't fight Live
            assert cl.level == logging.DEBUG  # firehose lowered to fill the Serial pane
        assert root.handlers == [sentinel]  # restored on exit
        assert cl.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        root.propagate = saved_prop
        cl.setLevel(saved_cl)


def test_run_dashboard_restores_on_exception() -> None:
    """The handler/firehose restoration runs even when the body raises."""
    s = DashboardState()
    console = Console(file=io.StringIO(), force_terminal=True, width=80, height=24)
    root = logging.getLogger("testrange")
    cl = logging.getLogger(CONSOLE_LOGGER)
    saved_handlers, saved_cl = root.handlers[:], cl.level
    sentinel = logging.NullHandler()
    root.handlers[:] = [sentinel]
    cl.setLevel(logging.WARNING)
    try:
        with (
            pytest.raises(RuntimeError, match="boom"),
            run_dashboard(s, enabled=True, console=console),
        ):
            raise RuntimeError("boom")
        assert root.handlers == [sentinel]
        assert cl.level == logging.WARNING
    finally:
        root.handlers[:] = saved_handlers
        cl.setLevel(saved_cl)
