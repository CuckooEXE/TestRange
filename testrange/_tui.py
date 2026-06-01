"""BuildKit-style collapsing live-tail renderer for streaming output (CORE-6).

Pure stdlib (CORE-18: no rich/tqdm dependency). A :class:`LiveTail` is a
``logging.Handler`` that, on an interactive terminal, renders streaming records
into a fixed-height region redrawn in place — so a build's serial firehose (and
a test's stdout) shows only its most-recent lines instead of scrolling the whole
screen. Infrequent progress records are *committed* as permanent lines above the
region; a step boundary collapses the region to a one-line summary
(``=> build web  DONE 47s``).

Mirrors :class:`testrange._progress.ProgressReporter`'s TTY/non-TTY split: off a
TTY (CI, piped, redirected) it degrades to plain per-line logging, no escapes.

Transient vs permanent is decided by logger name: records from the
:data:`CONSOLE_LOGGER` (build serial chatter) and :data:`TESTOUT_LOGGER`
(per-test stdout/stderr) loggers are the firehose, shown in the region;
everything else on the ``testrange`` tree is permanent progress. A transient
record names its step so the region can collapse on a boundary — explicitly via
a ``tr_step`` attribute, or for console records implicitly from the first
positional arg (``_ConsoleStreamer`` logs ``"[%s] %s", vm_name, line``).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from time import monotonic
from types import FrameType
from typing import IO

from testrange._ansi import scrub_terminal_control

CONSOLE_LOGGER = "testrange.orchestrator.build_phase.console"
TESTOUT_LOGGER = "testrange.orchestrator.runner.testout"

_DEFAULT_HEIGHT = 15
_ROOT_LOGGER = "testrange"


class LiveTail(logging.Handler):
    """A logging handler that renders streaming records as a collapsing tail."""

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        height: int = _DEFAULT_HEIGHT,
        width: int | None = None,
        tty: bool | None = None,
        now: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__()
        self._stream = stream if stream is not None else sys.stderr
        self._tty = self._stream.isatty() if tty is None else tty
        self._now = now
        self._height = max(1, height)
        self._width = width
        self._ring: deque[str] = deque(maxlen=self._height)
        self._drawn = 0  # lines currently occupied by the live region
        self._step: str | None = None
        self._step_start = now()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            if _is_transient(record):
                self._feed(text, _step_of(record))
            else:
                self.commit(text)
        except Exception:  # never let a render error escape into the log call
            self.handleError(record)

    def resize(self, *, width: int | None, height: int) -> None:
        """Adopt a new terminal size (SIGWINCH); re-cap the ring to ``height``."""
        self._width = width
        self._height = max(1, height)
        self._ring = deque(self._ring, maxlen=self._height)
        if self._tty:
            self._redraw()

    def finish(self, *, ok: bool = True) -> None:
        """Collapse the in-flight step and restore the terminal (idempotent).

        Called once on the way out — including from an exception handler, where
        ``ok=False`` marks the in-flight step ``FAIL`` rather than ``DONE``.
        """
        if self._step is not None:
            self._collapse(ok=ok)
        if self._tty:
            self._erase_region()
            self._write("\x1b[?25h")  # ensure the cursor is visible again

    def _feed(self, text: str, step: str | None) -> None:
        if step is not None and step != self._step:
            if self._step is not None:
                self._collapse(ok=True)
            self._step = step
            self._step_start = self._now()
        if not self._tty:
            for line in text.split("\n"):
                self._write(line + "\n")
            return
        for line in text.split("\n"):
            self._ring.append(line)
        self._redraw()

    def _redraw(self) -> None:
        visible = list(self._ring)
        parts: list[str] = []
        if self._drawn:
            parts.append(f"\x1b[{self._drawn}A")
        parts.append("\r")
        for line in visible:
            parts.append(self._truncate(line) + "\x1b[K\n")
        parts.append("\x1b[J")  # wipe any lines a now-shorter region left behind
        self._write("".join(parts))
        self._drawn = len(visible)

    def _erase_region(self) -> None:
        if self._drawn:
            self._write(f"\x1b[{self._drawn}A\r\x1b[J")
            self._drawn = 0

    def _truncate(self, line: str) -> str:
        # A wrapped line would desync the cursor-up count, so hard-cap to width.
        return line[: self._width] if self._width else line

    def commit(self, text: str) -> None:
        """Print ``text`` as a permanent line above the live region."""
        if not self._tty:
            self._write(text + "\n")
            return
        self._erase_region()
        self._write(text + "\n")
        self._redraw()

    def _collapse(self, *, ok: bool) -> None:
        elapsed = self._now() - self._step_start
        verdict = "DONE" if ok else "FAIL"
        summary = f"=> {self._step}  {verdict} {elapsed:.0f}s"
        if self._tty:
            self._erase_region()
        self._write(summary + "\n")
        self._ring.clear()
        self._step = None

    def _write(self, s: str) -> None:
        self._stream.write(s)
        self._stream.flush()


def _is_transient(record: logging.LogRecord) -> bool:
    return record.name.endswith((".console", ".testout"))


def _step_of(record: logging.LogRecord) -> str | None:
    """The step a transient record belongs to, or ``None`` to keep the current one."""
    explicit = getattr(record, "tr_step", None)
    if explicit:
        return str(explicit)
    if record.name == CONSOLE_LOGGER and record.args:
        # _ConsoleStreamer logs "[%s] %s", vm_name, line — the VM names the step.
        first = record.args[0] if isinstance(record.args, tuple) else None
        if first is not None:
            return f"build {first}"
    return None


def _term_size(stream: IO[str]) -> tuple[int, int]:
    """``(columns, lines)`` of ``stream``'s terminal; a sane default if unknown."""
    try:
        size = os.get_terminal_size(stream.fileno())
    except (OSError, ValueError):
        return 80, 24
    return size.columns, size.lines


def _region_height(lines: int) -> int:
    """Cap the live region to leave the prompt/scrollback room above it."""
    return max(1, min(_DEFAULT_HEIGHT, lines - 1))


@contextmanager
def live_output(*, verbose: bool, stream: IO[str] | None = None) -> Iterator[LiveTail | None]:
    """Route build/test streaming output through a collapsing live tail.

    When ``verbose`` is off this is a no-op (the normal stderr log handler stays
    in charge). When on:

    - **non-TTY** (CI, piped) — bump the console/test-output loggers to DEBUG so
      their lines print plainly through the existing handler; no region.
    - **TTY** — install a :class:`LiveTail` as the sole handler on the
      ``testrange`` tree for the duration (so it and the plain handler can't
      fight over the terminal), wire SIGWINCH to it, and tear it down — cursor
      restored, in-flight step collapsed ``DONE``/``FAIL`` — on the way out,
      including when the body raises.
    """
    stream = stream if stream is not None else sys.stderr
    if not verbose:
        yield None
        return

    console = logging.getLogger(CONSOLE_LOGGER)
    testout = logging.getLogger(TESTOUT_LOGGER)
    prev_levels = (console.level, testout.level)
    console.setLevel(logging.DEBUG)
    testout.setLevel(logging.DEBUG)

    if not stream.isatty():
        try:
            yield None
        finally:
            console.setLevel(prev_levels[0])
            testout.setLevel(prev_levels[1])
        return

    root = logging.getLogger(_ROOT_LOGGER)
    saved = root.handlers[:]
    cols, lines = _term_size(stream)
    tail = LiveTail(stream, height=_region_height(lines), width=cols)
    tail.setLevel(logging.DEBUG)
    for h in saved:
        root.removeHandler(h)
    root.addHandler(tail)
    prev_winch = _install_sigwinch(tail, stream)

    ok = True
    try:
        yield tail
    except BaseException:
        ok = False
        raise
    finally:
        _restore_sigwinch(prev_winch)
        tail.finish(ok=ok)
        root.removeHandler(tail)
        for h in saved:
            root.addHandler(h)
        console.setLevel(prev_levels[0])
        testout.setLevel(prev_levels[1])


def _install_sigwinch(tail: LiveTail, stream: IO[str]) -> object | None:
    """Wire SIGWINCH to ``tail.resize``; return the prior handler to restore.

    Only the main thread can install signal handlers, and SIGWINCH is
    POSIX-only — both guarded, so a worker thread or Windows just keeps the
    initial size.
    """
    if not hasattr(signal, "SIGWINCH") or threading.current_thread() is not threading.main_thread():
        return None

    def _on_winch(_signum: int, _frame: FrameType | None) -> None:
        cols, lines = _term_size(stream)
        tail.resize(width=cols, height=_region_height(lines))

    return signal.signal(signal.SIGWINCH, _on_winch)


def _restore_sigwinch(prev: object | None) -> None:
    if prev is not None and hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, prev)  # type: ignore[arg-type]


class _LineLogWriter:
    """A text sink that turns each written line into a ``TESTOUT_LOGGER`` record.

    Used to tee a test function's ``stdout``/``stderr`` into the live tail under
    ``--verbose``. Lines are scrubbed of terminal control bytes (a test that
    prints raw guest output shouldn't hijack the terminal either) and tagged
    with the test name so the region collapses per-test.
    """

    def __init__(self, logger: logging.Logger, step: str) -> None:
        self._log = logger
        self._step = step
        self._buf = ""
        self._emitting = False

    def write(self, s: str) -> int:
        if self._emitting:
            # A logging side-effect re-entered us — e.g. a handler's emit failed
            # and logging.handleError is writing the traceback to sys.stderr,
            # which we've redirected here. Don't recurse: pass straight to the
            # real stderr (or drop it if there is none).
            return sys.__stderr__.write(s) if sys.__stderr__ is not None else len(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def _emit(self, line: str) -> None:
        self._emitting = True
        try:
            self._log.info("%s", scrub_terminal_control(line), extra={"tr_step": self._step})
        finally:
            self._emitting = False


@contextmanager
def capture_test_output(test_name: str) -> Iterator[None]:
    """Tee the body's ``stdout``/``stderr`` into the live tail as ``test_name``'s step."""
    writer = _LineLogWriter(logging.getLogger(TESTOUT_LOGGER), test_name)
    with redirect_stdout(writer), redirect_stderr(writer):
        try:
            yield
        finally:
            writer.flush()


__all__ = [
    "CONSOLE_LOGGER",
    "TESTOUT_LOGGER",
    "LiveTail",
    "capture_test_output",
    "live_output",
]
