"""Unit tests for the transfer-progress reporter (PVE-15).

The reporter is pure stdlib and fully injectable: a fake clock drives the
throttle, a fake stream captures TTY redraws, and a fake logger captures the
non-TTY progress lines. No real I/O, no sleeps.
"""

from __future__ import annotations

import io

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
    r = ProgressReporter(total, "upload x", log=log, now=clk, **kw)  # type: ignore[arg-type]
    return r, clk, log


def test_non_tty_throttles_then_emits() -> None:
    r, clk, log = _reporter(100 * _MIB, interval=1.0, tty=False)
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
    r, clk, log = _reporter(100 * _MIB, interval=1.0, tty=False)
    clk.advance(0.1)
    r.update(60 * _MIB)  # below interval, suppressed
    r.finish()  # final is unconditional
    assert len(log.lines) == 1
    assert "60.0/100.0 MiB" in log.lines[0]
    assert "(60%)" in log.lines[0]


def test_instantaneous_rate_reflects_window() -> None:
    r, clk, log = _reporter(100 * _MIB, interval=1.0, tty=False)
    clk.advance(2.0)
    r.update(20 * _MIB)  # 20 MiB in 2 s → 10 MiB/s
    assert "10.00 MiB/s" in log.lines[0]


def test_tty_redraws_with_carriage_return_and_trailing_newline() -> None:
    clk = _Clock()
    buf = io.StringIO()
    r = ProgressReporter(100 * _MIB, "up", stream=buf, now=clk, interval=1.0, tty=True)
    clk.advance(2.0)
    r.update(50 * _MIB)
    out = buf.getvalue()
    assert out.startswith("\r")
    assert "[" in out and "%" in out  # a bar was drawn
    r.finish()
    assert buf.getvalue().endswith("\n")


def test_unknown_total_omits_percent() -> None:
    r, clk, log = _reporter(None, interval=1.0, tty=False)
    clk.advance(1.0)
    r.update(7 * _MIB)
    r.finish()
    joined = "\n".join(log.lines)
    assert "MiB" in joined
    assert "%" not in joined
