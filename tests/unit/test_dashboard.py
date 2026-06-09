"""Unit tests for the rich dashboard renderer + log handler (ADR-0029).

Rendering is captured against a force-terminal Console writing to a buffer, so no
real TTY is needed; the handler is driven with synthetic LogRecords shaped like
the real firehose.
"""

from __future__ import annotations

import io
import logging
import os
import termios
import time
from threading import Event, Thread

import pytest
from rich.console import Console

from testrange._dashboard import (
    _MAX_SCROLL,
    DashboardLogHandler,
    _dispatch_key,
    _footer,
    _key_reader,
    _ScrollState,
    _Tail,
    render,
    run_dashboard,
)
from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER
from testrange.orchestrator.dashboard_state import DashboardState, VMStage


def _capture(renderable: object, *, width: int = 120, height: int = 40) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=width, height=height)
    console.print(renderable)
    return buf.getvalue()


def _tail_window(lines: list[str], *, offset: int, height: int) -> list[str]:
    """The text rows a ``_Tail`` yields for a region of ``height`` rows.

    ``_Tail`` reads its height from the ``Layout`` region's ConsoleOptions, so the
    windowing logic is driven here with an explicit-height options object.
    """
    console = Console(width=40, height=height)
    options = console.options.update(height=height)
    rows = console.render_lines(_Tail(lines, offset=offset), options, pad=False)
    return ["".join(seg.text for seg in row).rstrip() for row in rows]


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


_LINES100 = [f"L{i}" for i in range(100)]


class TestScrollback:
    """Keyboard scrollback over the streaming panes (CORE-87)."""

    def test_offset_zero_shows_the_live_tail(self) -> None:
        assert _tail_window(_LINES100, offset=0, height=10) == [f"L{i}" for i in range(90, 100)]

    def test_positive_offset_scrolls_up(self) -> None:
        # Scrolled up 5 lines: the window ends 5 lines from the bottom.
        assert _tail_window(_LINES100, offset=5, height=10) == [f"L{i}" for i in range(85, 95)]

    def test_offset_clamps_at_the_top(self) -> None:
        # A huge offset (e.g. "jump to top") never runs off the top.
        assert _tail_window(_LINES100, offset=_MAX_SCROLL, height=10) == [
            f"L{i}" for i in range(10)
        ]

    def test_scroll_state_follows_tail_by_default(self) -> None:
        s = _ScrollState()
        assert s.focus == "log"
        assert s.offset("log") == 0 and s.offset("serial") == 0

    def test_arrow_keys_scroll_the_focused_pane(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"\x1b[A", s)  # up
        assert s.offset("log") == 1
        _dispatch_key(b"\x1b[A", s)
        assert s.offset("log") == 2
        _dispatch_key(b"\x1b[B", s)  # down
        assert s.offset("log") == 1

    def test_down_does_not_pass_the_live_tail(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"\x1b[B", s)  # down at the tail stays put
        assert s.offset("log") == 0

    def test_tab_and_arrows_cycle_focus(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"\t", s)
        assert s.focus == "serial"
        _dispatch_key(b"\x1b[C", s)  # right also cycles
        assert s.focus == "log"
        _dispatch_key(b"\x0c", s)  # Ctrl-L cycles
        assert s.focus == "serial"
        _dispatch_key(b"\x1b[D", s)  # left also cycles
        assert s.focus == "log"

    def test_jump_to_top_shows_top_not_the_sentinel(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"g", s)  # parks the offset at the jump-to-top sentinel
        assert s.offset("log") == _MAX_SCROLL
        footer = _footer(s).plain
        assert "Log: TOP" in footer  # shown as TOP, never "↑1000"
        assert "↑1000" not in footer

    def test_page_keys_move_a_page(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"\x1b[5~", s)  # PgUp
        assert s.offset("log") == 10
        _dispatch_key(b"\x1b[6~", s)  # PgDn
        assert s.offset("log") == 0

    def test_home_jumps_to_top_end_returns_to_live(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"g", s)  # top
        assert s.offset("log") == _MAX_SCROLL
        _dispatch_key(b"G", s)  # live
        assert s.offset("log") == 0
        _dispatch_key(b"\x1b[H", s)  # Home escape sequence → top
        assert s.offset("log") == _MAX_SCROLL
        _dispatch_key(b"\x1b[F", s)  # End escape sequence → live
        assert s.offset("log") == 0

    def test_unknown_keys_are_ignored(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"x", s)
        _dispatch_key(b"\x1b[Z", s)  # shift-tab, unmapped
        assert s.offset("log") == 0 and s.focus == "log"

    def test_footer_shows_keys_and_per_pane_status(self) -> None:
        s = _ScrollState()
        _dispatch_key(b"\t", s)  # focus serial
        _dispatch_key(b"\x1b[5~", s)  # scroll serial a page
        footer = _footer(s).plain
        assert "Tab" in footer
        assert "Log: LIVE" in footer
        assert "Serial: ↑10" in footer

    def test_scrolled_pane_title_shows_position(self) -> None:
        s = DashboardState()
        for i in range(200):
            s.append_log(f"log line {i}")
        scroll = _ScrollState()
        _dispatch_key(b"g", scroll)  # jump the focused (log) pane to the top
        out = _capture(render(s.snapshot(), scroll))
        assert "log line 0" in out  # the oldest line is now visible
        assert "↑" in out  # the title/footer marks a scrolled-back position


class _PtyStdin:
    """A stdin stand-in pointing ``_key_reader`` at a pty slave fd."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def isatty(self) -> bool:
        return True


def _icanon(fd: int) -> bool:
    return bool(termios.tcgetattr(fd)[3] & termios.ICANON)


def test_key_reader_cbreak_roundtrip_dispatches_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw-mode reader enters cbreak, dispatches a key, and restores the
    terminal on stop — exercised over a real pseudo-terminal (CORE-87)."""
    master, slave = os.openpty()
    try:
        saved = termios.tcgetattr(slave)  # cooked mode to begin with
        monkeypatch.setattr("sys.stdin", _PtyStdin(slave))
        scroll = _ScrollState()
        stop = Event()
        reader = Thread(target=_key_reader, args=(stop, scroll))
        reader.start()
        try:
            # Wait until cbreak is engaged (ICANON cleared) so the escape
            # sequence isn't line-buffered away before the reader sees it.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and _icanon(slave):
                time.sleep(0.01)
            assert not _icanon(slave), "reader never put the terminal into cbreak"

            os.write(master, b"\x1b[A")  # Up → scroll the focused (log) pane up one
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and scroll.offset("log") == 0:
                time.sleep(0.01)
            assert scroll.offset("log") == 1, "key was not dispatched"
        finally:
            stop.set()
            reader.join(timeout=3.0)
        assert not reader.is_alive(), "reader thread did not stop"
        assert termios.tcgetattr(slave) == saved, "terminal not restored to cooked mode"
    finally:
        os.close(master)
        os.close(slave)
