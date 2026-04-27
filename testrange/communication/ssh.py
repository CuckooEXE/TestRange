"""SSH communicator for Linux VMs using paramiko.

Communicates with a VM via SSH.  Suitable when the VM is reachable over
the network and has ``sshd`` running — typically the default on any
cloud-init-provisioned Linux image.

Compared to :class:`~testrange.communication.guest_agent.GuestAgentCommunicator`,
the SSH backend:

- Exercises the real network stack (useful for integration testing)
- Does not require any guest-agent package inside the VM
- Relies on the VM being network-reachable from the host

Requires ``paramiko``.  Install via the ``ssh`` extra::

    pip install testrange[ssh]
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

import paramiko

from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import SSHError, VMTimeoutError

_POLL_INTERVAL = 2.0
"""Seconds between connection attempts while waiting for ``sshd`` to accept."""

_SFTP_CHUNK_SIZE = 32768
"""Bytes transferred per SFTP read/write operation."""


class SSHCommunicator(AbstractCommunicator):
    """SSH-backed communicator implementing :class:`AbstractCommunicator`.

    :param host: Hostname or IP address of the VM.
    :param username: Login username.
    :param password: Optional login password.
    :param key_filename: Path to the private key file for key-based auth.
    :param port: TCP port (default ``22``).
    """

    _host: str
    """Remote hostname or IP address."""

    _username: str
    """Login username used when opening the SSH session."""

    _password: str | None
    """Optional login password; ``None`` when key-based auth is in use."""

    _key_filename: str | None
    """Optional path to the private key file; ``None`` for password auth."""

    _port: int
    """TCP port for the SSH connection."""

    _client: paramiko.SSHClient | None
    """Underlying paramiko client; ``None`` until :meth:`wait_ready` succeeds."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        key_filename: str | Path | None = None,
        port: int = 22,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._key_filename = str(key_filename) if key_filename else None
        self._port = port
        self._client = None

    def _require_client(self) -> paramiko.SSHClient:
        """Return the active client or raise if :meth:`wait_ready` never ran.

        :returns: The live :class:`paramiko.SSHClient`.
        :raises SSHError: If no session has been established.
        """
        if self._client is None:
            raise SSHError(
                "SSH session not established — call wait_ready() first."
            )
        return self._client

    def wait_ready(self, timeout: int = 120) -> None:
        """Poll for a working SSH connection until *timeout* expires.

        Re-attempts a fresh connection every :data:`_POLL_INTERVAL` seconds
        until the server accepts authentication or the deadline passes.

        :param timeout: Maximum seconds to wait.
        :raises VMTimeoutError: If ``sshd`` remains unreachable after *timeout*.
        """
        deadline = time.monotonic() + timeout
        attempt_timeout = max(_POLL_INTERVAL * 2, 5.0)
        last_exc: Exception | None = None

        while time.monotonic() < deadline:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    key_filename=self._key_filename,
                    timeout=attempt_timeout,
                    banner_timeout=attempt_timeout,
                    auth_timeout=attempt_timeout,
                    # Allow discovery of the private key that matches
                    # the Credential.ssh_key cloud-init authorized on
                    # the guest: ssh-agent first (most common), then
                    # ~/.ssh/id_{ed25519,rsa,ecdsa,...} as fallback.
                    # Mirrors how a user's interactive ``ssh`` finds
                    # the same key, so behaviour lines up with their
                    # expectations — and Debian cloud images disable
                    # root password SSH, so key-based auth is the only
                    # path that actually works there.
                    allow_agent=True,
                    look_for_keys=True,
                )
                self._client = client
                return
            except (paramiko.SSHException, OSError, EOFError) as exc:
                last_exc = exc
                client.close()
                time.sleep(_POLL_INTERVAL)

        raise VMTimeoutError(
            f"SSH server at {self._host}:{self._port} not ready after "
            f"{timeout}s (last error: {last_exc})"
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Execute *argv* over SSH and return captured output.

        Environment variables are injected by prepending ``env VAR=val ...``
        to the command, avoiding reliance on ``AcceptEnv`` in ``sshd_config``.

        :param argv: Command and arguments list.
        :param env: Extra environment variables.
        :param timeout: Maximum seconds to wait for the command to exit.
        :returns: :class:`ExecResult` with exit code and captured output.
        :raises VMTimeoutError: If the command does not exit within *timeout*.
        :raises SSHError: On any SSH protocol error.
        """
        client = self._require_client()

        parts: list[str] = []
        if env:
            parts.append("env")
            parts.extend(f"{k}={v}" for k, v in env.items())
        parts.extend(argv)
        command = " ".join(shlex.quote(p) for p in parts)

        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            stdin.close()
            stdout.channel.settimeout(timeout)
            try:
                out = stdout.read()
                err = stderr.read()
            except TimeoutError as exc:
                stdout.channel.close()
                raise VMTimeoutError(
                    f"SSH command timed out after {timeout}s: {argv!r}"
                ) from exc
            exit_code = stdout.channel.recv_exit_status()
        except paramiko.SSHException as exc:
            raise SSHError(f"SSH exec failed for {argv!r}: {exc}") from exc

        return ExecResult(exit_code=exit_code, stdout=out, stderr=err)

    def get_file(self, path: str) -> bytes:
        """Read a file from the VM via SFTP.

        :param path: Absolute path inside the VM.
        :returns: Raw file contents.
        :raises SSHError: On SFTP errors.
        """
        client = self._require_client()
        try:
            with client.open_sftp() as sftp, sftp.open(path, "rb") as fh:
                chunks: list[bytes] = []
                while True:
                    chunk = fh.read(_SFTP_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
        except (paramiko.SSHException, OSError) as exc:
            raise SSHError(f"SFTP read of {path!r} failed: {exc}") from exc

    def put_file(self, path: str, data: bytes) -> None:
        """Write *data* to *path* on the VM via SFTP.

        :param path: Absolute destination path inside the VM.
        :param data: Bytes to write.
        :raises SSHError: On SFTP errors.
        """
        client = self._require_client()
        try:
            with client.open_sftp() as sftp, sftp.open(path, "wb") as fh:
                for offset in range(0, len(data), _SFTP_CHUNK_SIZE):
                    fh.write(data[offset : offset + _SFTP_CHUNK_SIZE])
        except (paramiko.SSHException, OSError) as exc:
            raise SSHError(f"SFTP write of {path!r} failed: {exc}") from exc

    def hostname(self) -> str:
        """Return the guest hostname via the ``hostname`` command.

        :returns: Hostname string with trailing newline stripped.
        :raises SSHError: On SSH errors.
        """
        result = self.exec(["hostname"])
        return result.stdout.decode("utf-8", errors="replace").strip()

    def close(self) -> None:
        """Close the underlying SSH session.

        Safe to call multiple times.
        """
        if self._client is not None:
            self._client.close()
            self._client = None
