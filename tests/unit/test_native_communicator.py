"""NativeCommunicator — thin shim that delegates to driver-supplied callables."""

from __future__ import annotations

from typing import Any

import pytest

from testrange.communicators import NativeCommunicator
from testrange.exceptions import (
    CommunicatorAlreadyBoundError,
    CommunicatorClosedError,
    CommunicatorError,
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
    def test_close_is_idempotent(self) -> None:
        c, _ = _bound()
        c.close()
        c.close()  # second close must not raise

    def test_is_bound_false_after_close(self) -> None:
        c, _ = _bound()
        c.close()
        assert c.is_bound is False

    def test_execute_after_close_raises_closed(self) -> None:
        c, _ = _bound()
        c.close()
        with pytest.raises(CommunicatorClosedError, match="has been closed"):
            c.execute(["echo"])

    def test_read_file_after_close_raises_closed(self) -> None:
        c, _ = _bound()
        c.close()
        with pytest.raises(CommunicatorClosedError, match="has been closed"):
            c.read_file("/x")

    def test_write_file_after_close_raises_closed(self) -> None:
        c, _ = _bound()
        c.close()
        with pytest.raises(CommunicatorClosedError, match="has been closed"):
            c.write_file("/x", b"y")

    def test_rebind_after_close_raises_closed(self) -> None:
        c, rec = _bound()
        c.close()
        with pytest.raises(CommunicatorClosedError, match="has been closed"):
            c.bind(
                execute=rec.execute,
                read_file=rec.read_file,
                write_file=rec.write_file,
            )
