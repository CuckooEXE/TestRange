"""NativeCommunicator — thin shim that delegates to driver-supplied callables."""

from __future__ import annotations

from typing import Any

import pytest

from testrange.communicators import NativeCommunicator
from testrange.exceptions import (
    CommunicatorAlreadyBoundError,
    CommunicatorError,
    GuestAgentError,
)
from testrange.guest_io import ExecResult


class _Recorder:
    """A trio of GuestExec/GuestReadFile/GuestWriteFile-shaped callables."""

    def __init__(self) -> None:
        self.exec_calls: list[tuple[tuple[str, ...], float, str | None]] = []
        self.read_calls: list[str] = []
        self.write_calls: list[tuple[str, bytes]] = []

    def execute(self, argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
        self.exec_calls.append((tuple(argv), timeout, cwd))
        return ExecResult(exit_code=0, stdout=b"out", stderr=b"", duration=0.1)

    def read_file(self, path: str) -> bytes:
        self.read_calls.append(path)
        return b"file-contents"

    def write_file(self, path: str, data: bytes) -> None:
        self.write_calls.append((path, data))


def _bound() -> tuple[NativeCommunicator, _Recorder]:
    rec = _Recorder()
    c = NativeCommunicator()
    c.bind(execute=rec.execute, read_file=rec.read_file, write_file=rec.write_file)
    return c, rec


class TestBind:
    def test_unbound_by_default(self) -> None:
        assert NativeCommunicator().is_bound is False

    def test_bind_marks_bound(self) -> None:
        c, _ = _bound()
        assert c.is_bound is True

    def test_double_bind_raises(self) -> None:
        c, rec = _bound()
        with pytest.raises(CommunicatorAlreadyBoundError):
            c.bind(
                execute=rec.execute,
                read_file=rec.read_file,
                write_file=rec.write_file,
            )


class TestDelegation:
    def test_execute_delegates(self) -> None:
        c, rec = _bound()
        r = c.execute(["echo", "hi"], timeout=12.0, cwd="/tmp")
        assert r.exit_code == 0
        assert r.stdout == b"out"
        assert rec.exec_calls == [(("echo", "hi"), 12.0, "/tmp")]

    def test_read_file_delegates(self) -> None:
        c, rec = _bound()
        assert c.read_file("/etc/hostname") == b"file-contents"
        assert rec.read_calls == ["/etc/hostname"]

    def test_write_file_delegates(self) -> None:
        c, rec = _bound()
        c.write_file("/tmp/x", b"data")
        assert rec.write_calls == [("/tmp/x", b"data")]


class TestNotBound:
    def test_execute_unbound_raises(self) -> None:
        with pytest.raises(CommunicatorError, match="not bound"):
            NativeCommunicator().execute(["echo"])

    def test_read_file_unbound_raises(self) -> None:
        with pytest.raises(CommunicatorError, match="not bound"):
            NativeCommunicator().read_file("/x")

    def test_write_file_unbound_raises(self) -> None:
        with pytest.raises(CommunicatorError, match="not bound"):
            NativeCommunicator().write_file("/x", b"y")


class TestClose:
    """close() is a session reset, not terminal — mirrors SSHCommunicator.

    A Plan power-cycles a guest, closes the communicator to drop the stale
    session, and keeps using the same object; the next call re-establishes it
    (the portable lifecycle/snapshot idiom, REL-23).
    """

    def test_close_is_idempotent(self) -> None:
        c, _ = _bound()
        c.close()
        c.close()  # second close must not raise

    def test_stays_bound_after_close(self) -> None:
        c, _ = _bound()
        c.close()
        assert c.is_bound is True

    def test_execute_after_close_reconnects(self) -> None:
        # First call after a close() pings the agent (reconnect), then delegates.
        c, rec = _bound()
        c.close()
        r = c.execute(["echo", "hi"])
        assert r.exit_code == 0
        # A "true" readiness ping precedes the real command on the reconnect.
        assert rec.exec_calls[0][0] == ("true",)
        assert rec.exec_calls[-1][0] == ("echo", "hi")

    def test_read_file_after_close_reconnects(self) -> None:
        c, rec = _bound()
        c.close()
        assert c.read_file("/x") == b"file-contents"
        assert rec.exec_calls[0][0] == ("true",)  # readiness ping ran first

    def test_write_file_after_close_reconnects(self) -> None:
        c, rec = _bound()
        c.close()
        c.write_file("/x", b"y")
        assert rec.write_calls == [("/x", b"y")]
        assert rec.exec_calls[0][0] == ("true",)

    def test_reconnect_waits_for_agent_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The agent answers only on the 3rd ping after the reboot; the first two
        # raise GuestAgentError and the communicator keeps polling (no real sleep).
        monkeypatch.setattr("testrange.communicators.native.time.sleep", lambda _s: None)
        rec = _Recorder()
        pings = {"n": 0}

        def flaky(argv: Any, *, timeout: float = 60.0, cwd: str | None = None) -> ExecResult:
            if tuple(argv) == ("true",):
                pings["n"] += 1
                if pings["n"] < 3:
                    raise GuestAgentError("agent not connected")
            return rec.execute(argv, timeout=timeout, cwd=cwd)

        c = NativeCommunicator()
        c.bind(execute=flaky, read_file=rec.read_file, write_file=rec.write_file)
        c.close()
        assert c.execute(["echo"]).exit_code == 0
        assert pings["n"] == 3

    def test_rebind_after_close_still_raises_already_bound(self) -> None:
        # close() does not unbind, so re-binding to another VM is still rejected.
        c, rec = _bound()
        c.close()
        with pytest.raises(CommunicatorAlreadyBoundError):
            c.bind(
                execute=rec.execute,
                read_file=rec.read_file,
                write_file=rec.write_file,
            )
