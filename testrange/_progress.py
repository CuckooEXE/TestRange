"""Transfer-progress reporting for long byte-moving driver calls (PVE-15).

Pure stdlib. A :class:`ProgressReporter` throttles output so a slow or stalled
transfer is *visible* instead of silent: on an interactive stderr it redraws a
single bar line; otherwise (captured log, CI, backgrounded run) it logs periodic
``INFO`` lines carrying bytes / total / instantaneous rate — which is exactly
what turns "10 minutes of silence" into an immediate "uploading @ 0.6 MiB/s".

Fed by a transfer callback: pass :meth:`ProgressReporter.update` straight to a
callback reporting ``(transferred, total)`` — paramiko's ``sftp.get`` /
``sftp.put`` callback, which is how the driver moves volume bytes both ways.

Deliberately minimal: no retry, no stall-watchdog, no throughput floor (those
were scoped out of PVE-15). It only makes the transfer observable and reports a
final summary the caller can fold into an actionable error.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from time import monotonic
from typing import IO, Protocol

_MIB = 1024 * 1024
_BAR_WIDTH = 20


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
        stream: IO[str] | None = None,
        now: Callable[[], float] = monotonic,
        interval: float = 1.0,
        tty: bool | None = None,
    ) -> None:
        self._total = total
        self._label = label
        self._stream = stream if stream is not None else sys.stderr
        self._now = now
        self._interval = interval
        self._tty = self._stream.isatty() if tty is None else tty
        if log is not None:
            self._log: _SupportsInfo = log
        else:
            from testrange._log import get_logger

            self._log = get_logger(__name__)
        self._start = now()
        self._last_emit = self._start
        self._last_bytes = 0
        self._transferred = 0
        self._drew = False

    @property
    def transferred(self) -> int:
        return self._transferred

    def update(self, transferred: int) -> None:
        """Record cumulative bytes moved; emit a line if the throttle window elapsed."""
        self._transferred = transferred
        now = self._now()
        if now - self._last_emit >= self._interval:
            self._emit(now, final=False)

    def finish(self) -> None:
        """Emit the final summary unconditionally; close the TTY bar line."""
        self._emit(self._now(), final=True)
        if self._tty and self._drew:
            self._stream.write("\n")
            self._stream.flush()

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
        line = self._render(rate)
        if self._tty:
            self._stream.write("\r" + line)
            self._stream.flush()
            self._drew = True
        else:
            self._log.info(line)

    def _render(self, rate: float) -> str:
        done_mib = self._transferred / _MIB
        rate_mib = rate / _MIB
        if self._total:
            total_mib = self._total / _MIB
            frac = min(1.0, self._transferred / self._total)
            if self._tty:
                filled = int(_BAR_WIDTH * frac)
                bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
                return (
                    f"{self._label} [{bar}] {frac * 100:5.1f}% "
                    f"{done_mib:.1f}/{total_mib:.1f} MiB {rate_mib:6.2f} MiB/s"
                )
            return (
                f"{self._label}: {done_mib:.1f}/{total_mib:.1f} MiB "
                f"({frac * 100:.0f}%) {rate_mib:.2f} MiB/s"
            )
        return f"{self._label}: {done_mib:.1f} MiB {rate_mib:.2f} MiB/s"


__all__ = ["ProgressReporter"]
