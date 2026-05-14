"""QGACommunicator — guest I/O through a hypervisor's native guest agent."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.communicators.base import Communicator, ExecResult
from testrange.exceptions import CommunicatorAlreadyBoundError, CommunicatorError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile


class QGACommunicator(Communicator):
    """Communicator backed by a hypervisor's native guest agent.

    Constructed with no Plan-time arguments — the agent's identity is the
    VM itself. At run-phase bring-up the orchestrator binds it with three
    VM-bound callables pulled from the driver; this class is a thin shim
    that delegates to them and never sees a driver type.
    """

    def __init__(self) -> None:
        self._bound = False
        self._execute: GuestExec | None = None
        self._read_file: GuestReadFile | None = None
        self._write_file: GuestWriteFile | None = None

    @property
    def is_bound(self) -> bool:
        return self._bound

    def bind(
        self,
        *,
        execute: GuestExec,
        read_file: GuestReadFile,
        write_file: GuestWriteFile,
    ) -> None:
        """Bind to a live VM's guest agent. Called by the orchestrator."""
        if self._bound:
            raise CommunicatorAlreadyBoundError(
                "QGACommunicator already bound; construct a fresh instance per VM"
            )
        self._execute = execute
        self._read_file = read_file
        self._write_file = write_file
        self._bound = True

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        if self._execute is None:
            raise CommunicatorError(
                "QGACommunicator is not bound; the orchestrator must call .bind() first"
            )
        return self._execute(argv, timeout=timeout, cwd=cwd)

    def read_file(self, path: str) -> bytes:
        if self._read_file is None:
            raise CommunicatorError(
                "QGACommunicator is not bound; the orchestrator must call .bind() first"
            )
        return self._read_file(path)

    def write_file(self, path: str, data: bytes) -> None:
        if self._write_file is None:
            raise CommunicatorError(
                "QGACommunicator is not bound; the orchestrator must call .bind() first"
            )
        self._write_file(path, data)

    def close(self) -> None:
        """Drop the bound callables. Idempotent."""
        self._execute = None
        self._read_file = None
        self._write_file = None
