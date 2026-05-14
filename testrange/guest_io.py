"""Guest-side I/O vocabulary — the callable shapes for running commands and
moving files inside a brought-up VM.

These Protocols are exactly the shapes of ``Communicator.execute`` /
``read_file`` / ``write_file``. They exist so a driver can hand its
native-guest-agent operations to the orchestrator as loose callables, and
so a builder's readiness hook can take an ``execute`` callable without ever
seeing a Communicator type. Nothing here imports a driver or a communicator;
both import from here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from testrange.communicators.base import ExecResult

__all__ = ["ExecResult", "GuestExec", "GuestReadFile", "GuestWriteFile"]


@runtime_checkable
class GuestExec(Protocol):
    """A callable that runs a command inside a guest and returns its result."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult: ...


@runtime_checkable
class GuestReadFile(Protocol):
    """A callable that reads a file from a guest as raw bytes."""

    def __call__(self, path: str) -> bytes: ...


@runtime_checkable
class GuestWriteFile(Protocol):
    """A callable that writes raw bytes to a file in a guest."""

    def __call__(self, path: str, data: bytes) -> None: ...
