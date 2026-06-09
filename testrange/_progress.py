"""Transfer-progress reporting for long byte-moving driver calls (PVE-15).

A :class:`ProgressReporter` makes a slow or stalled transfer *visible* instead of
silent. On an interactive terminal it drives a :class:`rich.progress.Progress`
bar (ADR-0029) redrawn in place; otherwise (captured log, CI, backgrounded run)
it logs periodic ``INFO`` lines carrying bytes / total / instantaneous rate —
which is exactly what turns "10 minutes of silence" into an immediate "uploading
@ 0.6 MiB/s". That non-TTY periodic line is the CI/build-farm visibility CORE-18
deliberately kept (rich's bar alone goes silent off a terminal), so it survives
the rich migration unchanged.

Fed by a transfer callback: pass :meth:`ProgressReporter.update` straight to a
callback reporting cumulative bytes — paramiko's ``sftp.get`` / ``sftp.put``
callback, which is how the driver moves volume bytes both ways.

Deliberately minimal: no retry, no stall-watchdog, no throughput floor (those
were scoped out of PVE-15). It only makes the transfer observable and reports a
final summary the caller can fold into an actionable error
(:meth:`elapsed` / :meth:`avg_rate_mib`).
"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Protocol

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)

_MIB = 1024 * 1024


class _SupportsInfo(Protocol):
    def info(self, msg: object, *args: object) -> None: ...


class ProgressReporter:
    """Throttled progress for a single transfer of ``total`` bytes (``None`` if unknown)."""

    def __init__(
        self,
        total: int | None,
        label: str,
        *,
        log: _SupportsInfo | None = None,
        console: Console | None = None,
        now: Callable[[], float] = monotonic,
        interval: float = 1.0,
    ) -> None:
        self._total = total
        self._label = label
        self._now = now
        self._interval = interval
        if console is not None:
            self._console = console
        else:
            from testrange._console import err_console

            self._console = err_console()
        if log is not None:
            self._log: _SupportsInfo = log
        else:
            from testrange._log import get_logger

            self._log = get_logger(__name__)
        self._start = now()
        self._last_emit = self._start
        self._last_bytes = 0
        self._transferred = 0
        # The rich bar owns the terminal path; off a terminal it stays None and
        # update()/finish() fall through to the periodic-INFO log instead.
        self._progress: Progress | None = None
        self._task: TaskID | None = None
        if self._console.is_terminal:
            # auto_refresh=False → no background refresh thread: the bar redraws
            # only when update() is called (on each transfer callback), the same
            # redraw-on-data cadence the hand-rolled bar had, and deterministic.
            self._progress = Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeElapsedColumn(),
                console=self._console,
                auto_refresh=False,
            )
            self._progress.start()
            self._task = self._progress.add_task(label, total=total)

    def update(self, transferred: int) -> None:
        """Record cumulative bytes moved; redraw the bar (TTY) or emit a throttled line."""
        self._transferred = transferred
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, completed=transferred, refresh=True)
            return
        now = self._now()
        if now - self._last_emit >= self._interval:
            self._emit(now, final=False)

    def finish(self) -> None:
        """Close the bar (TTY) or emit the final summary line (non-TTY), unconditionally."""
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, completed=self._transferred, refresh=True)
            self._progress.stop()
            return
        self._emit(self._now(), final=True)

    def elapsed(self) -> float:
        return self._now() - self._start

    def avg_rate_mib(self) -> float:
        e = self.elapsed()
        return (self._transferred / _MIB) / e if e > 0 else 0.0

    def _emit(self, now: float, *, final: bool) -> None:
        if final:
            span = now - self._start
            rate = self._transferred / span if span > 0 else 0.0
        else:
            span = now - self._last_emit
            rate = (self._transferred - self._last_bytes) / span if span > 0 else 0.0
        self._last_emit = now
        self._last_bytes = self._transferred
        self._log.info(self._render(rate))

    def _render(self, rate: float) -> str:
        done_mib = self._transferred / _MIB
        rate_mib = rate / _MIB
        if self._total:
            total_mib = self._total / _MIB
            frac = min(1.0, self._transferred / self._total)
            return (
                f"{self._label}: {done_mib:.1f}/{total_mib:.1f} MiB "
                f"({frac * 100:.0f}%) {rate_mib:.2f} MiB/s"
            )
        return f"{self._label}: {done_mib:.1f} MiB {rate_mib:.2f} MiB/s"


__all__ = ["ProgressReporter"]
