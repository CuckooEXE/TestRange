"""NativeCommunicator — guest I/O through a hypervisor's native guest agent."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.communicators.base import Communicator, ExecResult
from testrange.exceptions import (
    CommunicatorAlreadyBoundError,
    CommunicatorClosedError,
    CommunicatorError,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile


class NativeCommunicator(Communicator):
    """Communicator backed by a hypervisor's native in-guest agent.

    Backend-agnostic by design: it fronts whatever native agent the driver
    exposes — QEMU Guest Agent (libvirt, Proxmox), VMware Tools guest-ops,
    Hyper-V integration / PowerShell Direct. Constructed with no Plan-time
    arguments — the agent's identity is the VM itself. At run-phase bring-up
    the orchestrator binds it with three VM-bound callables pulled from the
    driver; this class is a thin shim that delegates to them and never sees a
    driver type or backend wire protocol.
    """

    def __init__(self) -> None:
        # `_closed` is the only flag: "bound" is just "callables present and
        # not yet closed". Three nullable callables + one terminal flag, no
        # redundant `_bound` mirror.
        self._closed = False
        self._execute: GuestExec | None = None
        self._read_file: GuestReadFile | None = None
        self._write_file: GuestWriteFile | None = None

    @property
    def is_bound(self) -> bool:
        return self._execute is not None and not self._closed

    def bind(
        self,
        *,
        execute: GuestExec,
        read_file: GuestReadFile,
        write_file: GuestWriteFile,
    ) -> None:
        """Bind to a live VM's guest agent. Called by the orchestrator."""
        if self._closed:
            raise CommunicatorClosedError(
                "NativeCommunicator has been closed; construct a fresh instance per VM"
            )
        if self._execute is not None:
            raise CommunicatorAlreadyBoundError(
                "NativeCommunicator already bound; construct a fresh instance per VM"
            )
        self._execute = execute
        self._read_file = read_file
        self._write_file = write_file

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        self._check_usable()
        assert self._execute is not None  # narrowed by _check_usable
        return self._execute(argv, timeout=timeout, cwd=cwd)

    def read_file(self, path: str) -> bytes:
        self._check_usable()
        assert self._read_file is not None
        return self._read_file(path)

    def write_file(self, path: str, data: bytes) -> None:
        self._check_usable()
        assert self._write_file is not None
        self._write_file(path, data)

    def _check_usable(self) -> None:
        """Raise the right error for a closed or never-bound communicator."""
        if self._closed:
            raise CommunicatorClosedError(
                "NativeCommunicator has been closed; construct a fresh instance per VM"
            )
        if self._execute is None:
            raise CommunicatorError(
                "NativeCommunicator is not bound; the orchestrator must call .bind() first"
            )

    def close(self) -> None:
        """Close the communicator. Terminal and idempotent — a closed
        communicator cannot be re-bound (construct a fresh instance per VM)."""
        self._closed = True
        self._execute = None
        self._read_file = None
        self._write_file = None
