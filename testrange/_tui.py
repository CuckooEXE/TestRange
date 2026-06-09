"""Streaming-output plumbing for the run/build firehose (CORE-6, ADR-0029).

Two firehose loggers carry *raw guest output*: :data:`CONSOLE_LOGGER` (the build
serial console mirror) and :data:`TESTOUT_LOGGER` (a test function's teed
``stdout``/``stderr``). Both are quiesced to ``WARNING`` by default
(:func:`testrange._log._quiesce_firehose`) and surfaced only on demand — through
the live dashboard's Serial pane (:func:`testrange._dashboard.run_dashboard`, on a
TTY) or as plain ``--verbose`` log lines off a TTY.

This module owns the **test-output tee**: :func:`capture_test_output` redirects a
test's ``stdout``/``stderr`` into :data:`TESTOUT_LOGGER`, scrubbing terminal
control bytes first so a test that prints raw guest output cannot hijack the
terminal. (The build serial mirror is produced separately by the build phase's
``_ConsoleStreamer``.)
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from testrange._ansi import scrub_terminal_control

CONSOLE_LOGGER = "testrange.orchestrator.build_phase.console"
TESTOUT_LOGGER = "testrange.orchestrator.runner.testout"


class _LineLogWriter:
    """A text sink that turns each written line into a ``TESTOUT_LOGGER`` record.

    Used to tee a test function's ``stdout``/``stderr`` into the firehose under
    ``--verbose`` / the dashboard. Lines are scrubbed of terminal control bytes
    (a test that prints raw guest output shouldn't hijack the terminal either)
    and tagged with the test name.
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
        # Re-entrancy guard, mirroring write(): a logging side-effect can call
        # flush() mid-emit — e.g. a RichHandler installed on the testrange tree
        # renders the just-emitted record to the (redirected) stderr and flushes
        # it. Clear the buffer *before* emitting so that re-entrant flush sees
        # nothing, and bail outright while an emit is in flight; otherwise the
        # still-buffered partial line is re-emitted forever (infinite recursion).
        if self._emitting or not self._buf:
            return
        line, self._buf = self._buf, ""
        self._emit(line)

    def _emit(self, line: str) -> None:
        self._emitting = True
        try:
            self._log.info("%s", scrub_terminal_control(line), extra={"tr_step": self._step})
        finally:
            self._emitting = False


@contextmanager
def capture_test_output(test_name: str) -> Iterator[None]:
    """Tee the body's ``stdout``/``stderr`` into the firehose as ``test_name``'s step."""
    writer = _LineLogWriter(logging.getLogger(TESTOUT_LOGGER), test_name)
    with redirect_stdout(writer), redirect_stderr(writer):
        try:
            yield
        finally:
            writer.flush()


__all__ = [
    "CONSOLE_LOGGER",
    "TESTOUT_LOGGER",
    "capture_test_output",
]
