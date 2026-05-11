"""Communicator ABC and the shared ExecResult type.

Phase 0: the ABC + the result dataclass. Phase 5 wires SSHCommunicator
to paramiko. Per PLAN.md decision 5 there is **no** ``bind()`` on the ABC —
each concrete declares its own per-type bind signature.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a single Communicator.execute() call."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration: float

    @property
    def ok(self) -> bool:
        """True iff the exit code was 0."""
        return self.exit_code == 0


class Communicator(ABC):
    """Abstract communicator. Concretes are constructed with Plan-time args
    and bound at run-phase bring-up via their own per-type ``bind()`` method."""

    @abstractmethod
    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        """Run a command in the guest. Returns an ExecResult."""

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Read a file from the guest. Returns raw bytes."""

    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None:
        """Write a file to the guest. ``data`` is raw bytes."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection. Idempotent."""
