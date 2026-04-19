"""Abstract communicator interface for interacting with running VMs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NamedTuple


class ExecResult(NamedTuple):
    """The result of a command executed inside a VM.

    :param exit_code: The process exit code (``0`` typically means success).
    :param stdout: Raw bytes captured from the process's standard output.
    :param stderr: Raw bytes captured from the process's standard error.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        """Return :attr:`stdout` decoded as UTF-8 (replacing errors).

        :returns: Decoded stdout string.
        """
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        """Return :attr:`stderr` decoded as UTF-8 (replacing errors).

        :returns: Decoded stderr string.
        """
        return self.stderr.decode("utf-8", errors="replace")

    def check(self) -> ExecResult:
        """Raise :class:`RuntimeError` if :attr:`exit_code` is non-zero.

        :returns: ``self`` on success, for chaining.
        :raises RuntimeError: If exit code is not zero.
        """
        if self.exit_code != 0:
            raise RuntimeError(
                f"Command exited with code {self.exit_code}.\n"
                f"stderr: {self.stderr_text}"
            )
        return self


class AbstractCommunicator(ABC):
    """Abstract interface for communicating with a running VM.

    Concrete implementations must handle the transport details (e.g. QEMU
    guest agent over virtio-serial).  All methods that contact the VM raise
    :class:`~testrange.exceptions.VMNotRunningError` if the VM has not been
    started.

    Subclass this to support additional communication channels (e.g. SSH,
    WinRM).
    """

    @abstractmethod
    def wait_ready(self, timeout: int = 120) -> None:
        """Block until the communicator is ready to accept commands.

        :param timeout: Maximum seconds to wait before raising
            :class:`~testrange.exceptions.VMTimeoutError`.
        :raises VMTimeoutError: If the communicator does not become ready
            within *timeout* seconds.
        """

    @abstractmethod
    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Execute a command inside the VM and return its result.

        :param argv: Command and arguments list (e.g. ``['uname', '-n']``).
        :param env: Optional extra environment variables to set.
        :param timeout: Maximum seconds to wait for the command to finish.
        :returns: An :class:`ExecResult` with the exit code and captured
            output.
        :raises VMTimeoutError: If the command does not finish within
            *timeout* seconds.
        :raises CommunicationError: If the communicator returns an error.
        """

    @abstractmethod
    def get_file(self, path: str) -> bytes:
        """Read a file from the VM's filesystem.

        :param path: Absolute path inside the VM (e.g. ``'/etc/os-release'``).
        :returns: Raw file contents as bytes.
        :raises CommunicationError: If the file cannot be read.
        """

    @abstractmethod
    def put_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* inside the VM.

        :param path: Absolute destination path inside the VM.
        :param data: Bytes to write.
        :raises CommunicationError: If the file cannot be written.
        """

    @abstractmethod
    def hostname(self) -> str:
        """Return the VM's hostname as reported by the guest OS.

        :returns: Hostname string.
        :raises CommunicationError: On communication failure.
        """
