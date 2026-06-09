"""The live ``run``/``build`` dashboard renderer (ADR-0029).

Renders a :class:`~testrange.orchestrator.dashboard_state.DashboardSnapshot` into
a four-pane ``rich`` layout — VM lifecycle states, test pass/fail, a log tail, and
the build serial console — and feeds the two streaming panes from the logging
tree via :class:`DashboardLogHandler`.

This is the *only* module that imports both ``rich`` and the dashboard state, so
the renderer dependency never reaches the orchestrator's phase/driver code (which
imports just the rich-free state). The context-manager that owns the ``Live`` and
swaps handlers lives in CORE-77; this module is pure rendering + the handler.

The ``Live`` runs on the **alternate screen buffer** (``screen=True``, CORE-86):
VTE-based terminals (Terminator on Debian 13) flicker badly under the in-band
cursor-up redraw ``screen=False`` does, whereas the alt-buffer's controlled
full-screen repaint is flicker-free (the ``htop`` model). Because the alt-buffer
is torn down on exit (the final frame disappears), the caller prints a plain
summary afterwards.

The Log and Serial panes are **scrollable** (CORE-87): the panes normally follow
the tail, but a keyboard reader (:func:`_key_reader`, active only on a real TTY)
lets the user page back through the ring buffers. The scroll position is pure UI
state (:class:`_ScrollState`) and is deliberately kept *out* of the UI-agnostic
``DashboardState``.

Guest-influenced text (serial lines, test names) is rendered as
:class:`rich.text.Text` with markup disabled, so a guest line like ``[red]`` is
shown literally and can never inject rich markup — defence-in-depth on top of the
``_ansi`` scrub already applied upstream.
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import sys
import termios
import tty
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from threading import Event, Lock, Thread

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from testrange._console import err_console
from testrange._tui import CONSOLE_LOGGER, TESTOUT_LOGGER
from testrange.orchestrator.dashboard_state import DashboardSnapshot, DashboardState, VMStage

_ROOT_LOGGER = "testrange"

# Colour per lifecycle stage: terminal states stand out (green/red), in-flight
# stages share a working colour, PENDING is dim.
_STAGE_STYLE: dict[VMStage, str] = {
    VMStage.PENDING: "dim",
    VMStage.PROVISIONING: "cyan",
    VMStage.BUILDING: "yellow",
    VMStage.BOOTING: "cyan",
    VMStage.BINDING: "cyan",
    VMStage.READY: "bold green",
    VMStage.FAILED: "bold red",
}

_TEST_GLYPH: dict[str, tuple[str, str]] = {
    "running": ("…", "yellow"),
    "passed": ("✓", "green"),
    "failed": ("✗", "bold red"),
}

# Log-level → colour, mirroring RichHandler's level styling so the Log pane reads
# like the CI rich logs.
_LEVEL_STYLE: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "blue",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold red",
}

# The two streaming panes the user can scroll back through, in Tab-cycle order.
_SCROLLABLE: tuple[str, ...] = ("log", "serial")
# Upper bound on a pane's stored scroll offset. The renderer re-clamps to the
# actual line count for the pane's height, so this only needs to exceed the
# largest ring (serial = 500); "jump to top" parks the offset here and the
# renderer resolves it to the true top.
_MAX_SCROLL = 1000
# Border colour for the pane that currently has scroll focus.
_FOCUS_STYLE = "bold cyan"


class _ScrollState:
    """Thread-safe scrollback position for the two streaming panes (CORE-87).

    Pure UI state shared between the key-reader thread (writer) and the ``Live``
    refresh thread (reader). An offset is "lines scrolled up from the bottom";
    ``0`` means *follow the live tail*. Offsets are stored loosely bounded and
    re-clamped against the real line count at render time.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._focus = _SCROLLABLE[0]
        self._offsets: dict[str, int] = dict.fromkeys(_SCROLLABLE, 0)

    @property
    def focus(self) -> str:
        with self._lock:
            return self._focus

    def offset(self, pane: str) -> int:
        with self._lock:
            return self._offsets[pane]

    def cycle_focus(self) -> None:
        with self._lock:
            nxt = (_SCROLLABLE.index(self._focus) + 1) % len(_SCROLLABLE)
            self._focus = _SCROLLABLE[nxt]

    def scroll(self, delta: int) -> None:
        """Scroll the focused pane by ``delta`` lines (+up / -down)."""
        with self._lock:
            cur = self._offsets[self._focus]
            self._offsets[self._focus] = max(0, min(cur + delta, _MAX_SCROLL))

    def to_top(self) -> None:
        with self._lock:
            self._offsets[self._focus] = _MAX_SCROLL

    def to_live(self) -> None:
        with self._lock:
            self._offsets[self._focus] = 0


def _plain_line(line: str) -> Text:
    # markup=False via Text(): never interpret guest output as markup.
    return Text(line, no_wrap=True, overflow="ellipsis")


def _styled_log_line(line: str) -> Text:
    """Colour a ``LEVEL [run_id] name: message`` log line by its leading level.

    The level is always the first whitespace-delimited token and drawn from a
    closed set, so a guest-influenced *message* can never be mistaken for one.
    Lines without a known level prefix (e.g. directly-appended notes) render
    plain.
    """
    head, sep, rest = line.partition(" ")
    if sep and head in _LEVEL_STYLE:
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(f"{head:<8}", style=_LEVEL_STYLE[head])
        text.append(rest)
        return text
    return _plain_line(line)


class _Tail:
    """Render a height-sized window of a buffer, offset up from the most recent.

    A ``Layout`` region hands its height to the renderable via ``options``. With
    ``offset == 0`` the window is the last ``height`` lines, so the **most recent**
    output stays visible (a plain ``Text`` would be top-cropped). A positive
    ``offset`` scrolls the window up by that many lines (clamped so it never runs
    off the top), which is how the scrollable panes show earlier output (CORE-87).
    ``styler`` turns each raw line into a ``Text`` (default: plain, markup-safe).
    """

    def __init__(
        self,
        lines: Sequence[str],
        *,
        styler: Callable[[str], Text] = _plain_line,
        offset: int = 0,
    ) -> None:
        self._lines = lines
        self._styler = styler
        self._offset = offset

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        height = options.height if options.height is not None else options.max_height
        if not height:
            for line in self._lines:
                yield self._styler(line)
            return
        n = len(self._lines)
        max_off = max(0, n - height)
        eff = min(max(self._offset, 0), max_off)
        end = n - eff
        start = max(0, end - height)
        for line in self._lines[start:end]:
            yield self._styler(line)


def _vm_panel(snapshot: DashboardSnapshot) -> Panel:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column("vm", ratio=2, no_wrap=True)
    table.add_column("stage", ratio=2, no_wrap=True)
    table.add_column("elapsed", justify="right", ratio=1, no_wrap=True)
    for vm in snapshot.vms:
        elapsed = "-" if vm.elapsed is None else f"{vm.elapsed:.0f}s"
        detail = Text(vm.name)
        if vm.stage is VMStage.FAILED and vm.detail:
            detail = Text.assemble(vm.name, ("  ", ""), (vm.detail, "red"))
        table.add_row(
            detail,
            Text(str(vm.stage), style=_STAGE_STYLE.get(vm.stage, "")),
            Text(elapsed, style="dim"),
        )
    return Panel(table, title="VMs", border_style="blue")


def _tests_panel(snapshot: DashboardSnapshot) -> Panel:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column("status", no_wrap=True)
    table.add_column("test", ratio=1, no_wrap=True)
    table.add_column("dur", justify="right", no_wrap=True)
    passed = sum(t.status == "passed" for t in snapshot.tests)
    failed = sum(t.status == "failed" for t in snapshot.tests)
    for t in snapshot.tests:
        glyph, style = _TEST_GLYPH.get(t.status, ("?", ""))
        table.add_row(
            Text(glyph, style=style),
            Text(t.name),
            Text(f"{t.duration:.2f}s" if t.status != "running" else "", style="dim"),
        )
    title = f"Tests  [green]{passed} ✓[/]  [red]{failed} ✗[/]" if snapshot.tests else "Tests"
    return Panel(table, title=title, border_style="blue")


def _scroll_status(offset: int) -> str:
    """Compact scroll-position label: ``LIVE`` at the tail, ``TOP`` at the cap,
    else ``↑N``. (At the cap the stored offset is the jump-to-top sentinel, not a
    real line distance, so it is shown as ``TOP`` rather than ``↑1000``.)"""
    if offset <= 0:
        return "LIVE"
    if offset >= _MAX_SCROLL:
        return "TOP"
    return f"↑{offset}"


def _pane_title(name: str, offset: int) -> str:
    """A pane title that shows the scroll position when scrolled off the tail."""
    return name if offset <= 0 else f"{name}  {_scroll_status(offset)}"


def _stream_panel(
    name: str, lines: Sequence[str], scroll: _ScrollState, *, styler: Callable[[str], Text]
) -> Panel:
    pane = name.lower()
    offset = scroll.offset(pane)
    focused = scroll.focus == pane
    return Panel(
        _Tail(lines, styler=styler, offset=offset),
        title=_pane_title(name, offset),
        border_style=_FOCUS_STYLE if focused else "blue",
    )


def _footer(scroll: _ScrollState) -> Text:
    """One-line key hint + per-pane scroll position."""
    text = Text(no_wrap=True, overflow="ellipsis", style="dim")
    text.append("[Tab] pane  [↑↓] line  [PgUp/PgDn] page  [Home/End] top/live")
    for pane in _SCROLLABLE:
        style = _FOCUS_STYLE if scroll.focus == pane else "dim"
        text.append(f"   {pane.capitalize()}: {_scroll_status(scroll.offset(pane))}", style=style)
    return text


def render(snapshot: DashboardSnapshot, scroll: _ScrollState | None = None) -> RenderableType:
    """Build the dashboard layout from a snapshot (+ optional scroll state).

    The top row (VMs + Tests) takes 1/5 of the height and the streaming panes
    (Log + Serial) take 4/5 (CORE-88), with a one-line key/scroll footer beneath.
    """
    scroll = scroll if scroll is not None else _ScrollState()
    layout = Layout()
    layout.split_column(
        Layout(name="top", ratio=1),
        Layout(name="bottom", ratio=4),
        Layout(_footer(scroll), name="footer", size=1),
    )
    layout["top"].split_row(
        Layout(_vm_panel(snapshot), name="vms"),
        Layout(_tests_panel(snapshot), name="tests"),
    )
    layout["bottom"].split_row(
        Layout(
            _stream_panel("Log", snapshot.log_lines, scroll, styler=_styled_log_line), name="log"
        ),
        Layout(
            _stream_panel(
                "Serial",
                [f"{vm}  {line}" for vm, line in snapshot.serial_lines],
                scroll,
                styler=_plain_line,
            ),
            name="serial",
        ),
    )
    return layout


class DashboardLogHandler(logging.Handler):
    """Route logging records into the dashboard's log/serial ring buffers.

    Records from :data:`CONSOLE_LOGGER` (the build serial firehose, already
    scrubbed upstream) go to the serial pane tagged by VM; the per-test stdout
    tee (:data:`TESTOUT_LOGGER`) is dropped (the Tests pane already covers it);
    every other ``testrange.*`` record goes to the log pane as
    ``LEVEL [run_id] name: message`` — the same level/run_id/logger fields the
    ``RichHandler`` shows in CI, so the pane reads like the non-TTY logs (the
    level is colourised at render time by :func:`_styled_log_line`).
    """

    def __init__(self, state: DashboardState) -> None:
        super().__init__()
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == CONSOLE_LOGGER:
                vm, line = self._console_parts(record)
                self._state.append_serial(vm, line)
            elif record.name == TESTOUT_LOGGER:
                return
            else:
                self._state.append_log(self._log_line(record))
        except Exception:  # a render/format error must never escape the log call
            self.handleError(record)

    @staticmethod
    def _log_line(record: logging.LogRecord) -> str:
        # Same fields RichHandler renders (level + run_id correlator + logger),
        # short logger name for the narrow pane; the level leads so the renderer
        # can colour it. run_id is injected by _RunIdAdapter ("-" on raw records).
        run_id = getattr(record, "run_id", "-")
        name = record.name.split(".")[-1]
        return f"{record.levelname} [{run_id}] {name}: {record.getMessage()}"

    @staticmethod
    def _console_parts(record: logging.LogRecord) -> tuple[str, str]:
        # _ConsoleStreamer logs ``"[%s] %s", vm_name, line`` (line already scrubbed).
        if isinstance(record.args, tuple) and len(record.args) == 2:
            return str(record.args[0]), str(record.args[1])
        return "-", record.getMessage()


# Arrow / navigation escape sequences (the bytes after the leading ESC), mapped
# to a logical key. Terminals differ on Home/End, so several spellings map to one.
_NAV_KEYS: dict[bytes, str] = {
    b"[A": "up",
    b"[B": "down",
    b"[C": "right",
    b"[D": "left",
    b"[5~": "pgup",
    b"[6~": "pgdn",
    b"[H": "home",
    b"[F": "end",
    b"[1~": "home",
    b"[4~": "end",
    b"OH": "home",
    b"OF": "end",
}


def _dispatch_key(chunk: bytes, scroll: _ScrollState) -> None:
    """Apply one read of stdin to the scroll state."""
    if chunk in (b"\t", b"\x0c"):  # Tab / Ctrl-L → next pane
        scroll.cycle_focus()
        return
    if chunk == b"g":
        scroll.to_top()
        return
    if chunk == b"G":
        scroll.to_live()
        return
    if chunk[:1] != b"\x1b":
        return
    key = _NAV_KEYS.get(chunk[1:])
    if key == "up":
        scroll.scroll(1)
    elif key == "down":
        scroll.scroll(-1)
    elif key == "pgup":
        scroll.scroll(10)  # a "page" is ~10 lines
    elif key == "pgdn":
        scroll.scroll(-10)
    elif key == "home":
        scroll.to_top()
    elif key == "end":
        scroll.to_live()
    elif key in ("left", "right"):
        scroll.cycle_focus()


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):  # pragma: no cover - stdin detached
        return False


def _key_reader(stop: Event, scroll: _ScrollState) -> None:
    """Read scroll keys from stdin in cbreak mode until ``stop`` is set.

    Runs on a daemon thread only when stdin is a real TTY. The terminal mode is
    saved on entry and restored on every exit path; any failure to grab the
    terminal disables scrolling silently (the dashboard still renders).
    """
    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (OSError, ValueError, termios.error):  # pragma: no cover - no usable tty
        return
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 8)
            except OSError:  # pragma: no cover - tty went away mid-run
                break
            if not chunk:
                break
            _dispatch_key(chunk, scroll)
    finally:
        with contextlib.suppress(OSError, termios.error):  # pragma: no cover
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


@contextmanager
def run_dashboard(
    state: DashboardState,
    *,
    enabled: bool,
    console: Console | None = None,
    verbose: bool = False,
) -> Iterator[DashboardState | None]:
    """Own the terminal with the live dashboard for the duration of a run/build.

    The dashboard activates only when ``enabled`` *and* the console is a real
    terminal. When active it takes over the ``testrange`` logger — removing the
    existing handler (so it can't fight the ``Live`` for the screen, the same
    reason the old live tail did), installing a :class:`DashboardLogHandler` that
    feeds the panes, lowering the serial firehose so the Serial pane fills — runs
    on the alternate screen buffer (flicker-free, CORE-86), and starts a key
    reader for scrollback when stdin is a TTY (CORE-87) — and restores
    everything on the way out, **including the exception path**.

    When inactive (no TTY, piped, or ``--no-dashboard``), it yields ``None`` and
    logging keeps flowing through the already-installed ``RichHandler`` unchanged;
    ``verbose`` still lowers the firehose so a piped/CI run can see serial/test
    output as plain log lines.
    """
    console = console if console is not None else err_console()
    firehose = [logging.getLogger(CONSOLE_LOGGER), logging.getLogger(TESTOUT_LOGGER)]
    prev_levels = [lg.level for lg in firehose]
    active = enabled and console.is_terminal

    if not active:
        if verbose:
            for lg in firehose:
                lg.setLevel(logging.DEBUG)
        try:
            yield None
        finally:
            for lg, lvl in zip(firehose, prev_levels, strict=True):
                lg.setLevel(lvl)
        return

    root = logging.getLogger(_ROOT_LOGGER)
    saved = root.handlers[:]
    handler = DashboardLogHandler(state)
    handler.setLevel(logging.DEBUG)
    scroll = _ScrollState()
    stop = Event()
    reader: Thread | None = None
    # Everything that mutates global state — the handler swap, the firehose
    # levels, the cbreak key-reader thread, and the Live (whose construction
    # eagerly renders once) — runs inside the try so the finally restores it all
    # on *any* failure, including a render error during Live construction. A
    # leaked reader thread would otherwise strand the terminal in cbreak.
    try:
        for h in saved:
            root.removeHandler(h)
        root.addHandler(handler)
        for lg in firehose:
            lg.setLevel(logging.DEBUG)  # let the serial firehose reach the pane
        if _stdin_is_tty():
            reader = Thread(target=_key_reader, args=(stop, scroll), daemon=True)
            reader.start()
        live = Live(
            console=console,
            screen=True,
            refresh_per_second=8,
            get_renderable=lambda: render(state.snapshot(), scroll),
        )
        with live:
            yield state
    finally:
        stop.set()
        if reader is not None:
            reader.join(timeout=1.0)
        root.removeHandler(handler)
        for h in saved:
            root.addHandler(h)
        for lg, lvl in zip(firehose, prev_levels, strict=True):
            lg.setLevel(lvl)


__all__ = ["DashboardLogHandler", "render", "run_dashboard"]
