"""Unit tests for wait_communicator_ready (the per-VM agent-readiness gate).

At realize boot the native guest agent comes up a few seconds after power-on;
the first exec must not race it (PVE returns "QEMU guest agent is not running").
These drive the poll loop with a fake clock — no sleeps, no live backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from testrange.exceptions import CommunicatorError, GuestAgentError, OrchestratorError
from testrange.orchestrator import vm_run


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


class _Comm:
    """Fails its first `fail_times` execs with `exc`, then answers."""

    def __init__(self, fail_times: int, exc: type[Exception] = GuestAgentError) -> None:
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc

    def execute(self, argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> Any:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc("agent not up yet")
        return SimpleNamespace(exit_code=0, stdout=b"", stderr=b"", duration=0.0)


def _vm(comm: _Comm) -> Any:
    return SimpleNamespace(name="web", communicator=comm)


def _ctx(*, timeout: float = 6.0) -> Any:
    return SimpleNamespace(agent_ready_timeout_s=timeout)


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(vm_run, "time", clock)
    return clock


def test_ready_on_first_try() -> None:
    comm = _Comm(fail_times=0)
    vm_run.wait_communicator_ready(_ctx(), _vm(comm))
    assert comm.calls == 1


def test_ready_after_transient_agent_errors() -> None:
    comm = _Comm(fail_times=2)  # fails at t=0,2 then answers at t=4 (< 6s budget)
    vm_run.wait_communicator_ready(_ctx(), _vm(comm))
    assert comm.calls == 3


def test_communicator_error_is_also_transient() -> None:
    comm = _Comm(fail_times=1, exc=CommunicatorError)
    vm_run.wait_communicator_ready(_ctx(), _vm(comm))
    assert comm.calls == 2


def test_never_ready_fails_loud_with_last_error() -> None:
    comm = _Comm(fail_times=999)
    with pytest.raises(OrchestratorError, match="communicator not ready"):
        vm_run.wait_communicator_ready(_ctx(timeout=6.0), _vm(comm))
    assert comm.calls >= 3  # polled across the whole budget before giving up
