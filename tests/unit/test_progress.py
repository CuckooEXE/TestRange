"""Unit tests for the transfer-progress reporter (PVE-15).

The non-TTY path (the CI/build-farm visibility CORE-18 kept, ADR-0029) is fully
injectable: a fake clock drives the throttle, a fake logger captures the
periodic progress lines, and a non-terminal :class:`rich.console.Console` selects
the log path. The TTY path drives a real :class:`rich.progress.Progress` against
a force-terminal Console capturing to a buffer. No real I/O, no sleeps.
"""

from __future__ import annotations

import io

from rich.console import Console

from testrange._progress import ProgressReporter

_MIB = 1024 * 1024


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg: object, *args: object) -> None:
        self.lines.append(str(msg) % args if args else str(msg))


def _reporter(total: int | None, **kw: object) -> tuple[ProgressReporter, _Clock, _Log]:
    clk = _Clock()
    log = _Log()
    # A non-terminal Console selects the periodic-INFO log path.
    console = Console(file=io.StringIO())
    r = ProgressReporter(total, "upload x", log=log, console=console, now=clk, **kw)  # type: ignore[arg-type]
    return r, clk, log


def test_non_tty_throttles_then_emits() -> None:
    r, clk, log = _reporter(100 * _MIB, interval=1.0)
    r.update(10 * _MIB)  # dt=0 since start → below interval, no line
    assert log.lines == []
    clk.advance(1.0)
    r.update(50 * _MIB)  # crosses the interval → one line
    assert len(log.lines) == 1
    assert "50.0/100.0 MiB" in log.lines[0]
    assert "MiB/s" in log.lines[0]
    clk.advance(0.4)
    r.update(60 * _MIB)  # still inside interval → suppressed
    assert len(log.lines) == 1


def test_finish_always_emits_final_with_actual_transferred() -> None:
    r, clk, log = _reporter(100 * _MIB, interval=1.0)
    clk.advance(0.1)
    r.update(60 * _MIB)  # below interval, suppressed
    r.finish()  # final is unconditional
    assert len(log.lines) == 1
    assert "60.0/100.0 MiB" in log.lines[0]
    assert "(60%)" in log.lines[0]


def test_instantaneous_rate_reflects_window() -> None:
    r, clk, log = _reporter(100 * _MIB, interval=1.0)
    clk.advance(2.0)
    r.update(20 * _MIB)  # 20 MiB in 2 s → 10 MiB/s
    assert "10.00 MiB/s" in log.lines[0]


def test_unknown_total_omits_percent() -> None:
    r, clk, log = _reporter(None, interval=1.0)
    clk.advance(1.0)
    r.update(7 * _MIB)
    r.finish()
    joined = "\n".join(log.lines)
    assert "MiB" in joined
    assert "%" not in joined


def test_tty_draws_a_bar_and_does_not_log() -> None:
    """On a terminal the reporter drives a rich bar, not the INFO log path."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=80)
    log = _Log()
    r = ProgressReporter(100 * _MIB, "uploading disk", log=log, console=console)
    r.update(50 * _MIB)
    r.finish()
    out = buf.getvalue()
    assert "uploading disk" in out  # the task description was rendered
    assert log.lines == []  # the non-TTY log path stayed silent
