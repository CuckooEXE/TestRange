"""Unit tests for wait_communicators_ready (the user-VM agent-readiness gate).

At run-phase boot the native guest agent comes up a few seconds after power-on;
the first exec must not race it (PVE returns "QEMU guest agent is not running").
These drive the poll loop with a fake clock — no sleeps, no live backend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from testrange.exceptions import CommunicatorError, GuestAgentError, OrchestratorError
from testrange.orchestrator import run_phase
from testrange.orchestrator.dashboard_state import DashboardState


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


def _ctx(comm: _Comm, *, timeout: float = 6.0) -> Any:
    vm = SimpleNamespace(name="web", communicator=comm)
    return SimpleNamespace(
        plan=SimpleNamespace(hypervisor=SimpleNamespace(vms=[vm])),
        agent_ready_timeout_s=timeout,
        jobs=1,  # single fake VM; keep the readiness poll on the calling thread
        dashboard=DashboardState(),  # the per-VM guard tags failures here
    )


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(run_phase, "time", clock)
    return clock


def test_ready_on_first_try() -> None:
    comm = _Comm(fail_times=0)
    run_phase.wait_communicators_ready(_ctx(comm))
    assert comm.calls == 1


def test_ready_after_transient_agent_errors() -> None:
    comm = _Comm(fail_times=2)  # fails at t=0,2 then answers at t=4 (< 6s budget)
    run_phase.wait_communicators_ready(_ctx(comm))
    assert comm.calls == 3


def test_communicator_error_is_also_transient() -> None:
    comm = _Comm(fail_times=1, exc=CommunicatorError)
    run_phase.wait_communicators_ready(_ctx(comm))
    assert comm.calls == 2


def test_never_ready_fails_loud_with_last_error() -> None:
    comm = _Comm(fail_times=999)
    with pytest.raises(OrchestratorError, match="communicator not ready"):
        run_phase.wait_communicators_ready(_ctx(comm, timeout=6.0))
    assert comm.calls >= 3  # polled across the whole budget before giving up
