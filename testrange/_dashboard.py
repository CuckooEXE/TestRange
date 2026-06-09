"""The live ``run``/``build`` dashboard renderer (ADR-0029).

Renders a :class:`~testrange.orchestrator.dashboard_state.DashboardSnapshot` into
a four-pane ``rich`` layout — VM lifecycle states, test pass/fail, a log tail, and
the build serial console — and feeds the two streaming panes from the logging
tree via :class:`DashboardLogHandler`.

This is the *only* module that imports both ``rich`` and the dashboard state, so
the renderer dependency never reaches the orchestrator's phase/driver code (which
imports just the rich-free state). The context-manager that owns the ``Live`` and
swaps handlers lives in CORE-77; this module is pure rendering + the handler.

Guest-influenced text (serial lines, test names) is rendered as
:class:`rich.text.Text` with markup disabled, so a guest line like ``[red]`` is
shown literally and can never inject rich markup — defence-in-depth on top of the
``_ansi`` scrub already applied upstream.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

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
    """Render the *last* lines of a buffer that fit the available region height.

    A ``Layout`` region hands its height to the renderable via ``options``, so
    slicing to the last ``height`` lines keeps the **most recent** output visible
    (a plain ``Text`` would be top-cropped, hiding the latest line). ``styler``
    turns each raw line into a ``Text`` (default: plain, markup-safe).
    """

    def __init__(
        self, lines: Sequence[str], *, styler: Callable[[str], Text] = _plain_line
    ) -> None:
        self._lines = lines
        self._styler = styler

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        height = options.height if options.height is not None else options.max_height
        visible = self._lines[-height:] if height else self._lines
        for line in visible:
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


def _log_panel(snapshot: DashboardSnapshot) -> Panel:
    return Panel(
        _Tail(snapshot.log_lines, styler=_styled_log_line), title="Log", border_style="blue"
    )


def _serial_panel(snapshot: DashboardSnapshot) -> Panel:
    lines = [f"{vm}  {line}" for vm, line in snapshot.serial_lines]
    return Panel(_Tail(lines), title="Serial (build)", border_style="blue")


def render(snapshot: DashboardSnapshot) -> RenderableType:
    """Build the four-pane dashboard layout from a snapshot."""
    layout = Layout()
    layout.split_column(Layout(name="top", ratio=1), Layout(name="bottom", ratio=1))
    layout["top"].split_row(
        Layout(_vm_panel(snapshot), name="vms"),
        Layout(_tests_panel(snapshot), name="tests"),
    )
    layout["bottom"].split_row(
        Layout(_log_panel(snapshot), name="log"),
        Layout(_serial_panel(snapshot), name="serial"),
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
    feeds the panes, lowering the serial firehose so the Serial pane fills — and
    restores everything on the way out, **including the exception path**.

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
    for h in saved:
        root.removeHandler(h)
    root.addHandler(handler)
    for lg in firehose:
        lg.setLevel(logging.DEBUG)  # let the serial firehose reach the pane

    live = Live(
        console=console,
        screen=False,
        refresh_per_second=8,
        get_renderable=lambda: render(state.snapshot()),
    )
    try:
        with live:
            yield state
    finally:
        root.removeHandler(handler)
        for h in saved:
            root.addHandler(h)
        for lg, lvl in zip(firehose, prev_levels, strict=True):
            lg.setLevel(lvl)


__all__ = ["DashboardLogHandler", "render", "run_dashboard"]
