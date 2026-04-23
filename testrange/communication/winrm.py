"""WinRM communicator for Windows VMs using pywinrm.

Communicates with a Windows VM over the WinRM protocol (HTTP 5985 or
HTTPS 5986).  The guest must have WinRM enabled;
:class:`~testrange.vms.builders.WindowsUnattendedBuilder` arranges this in
its first-logon commands via ``Enable-PSRemoting``.

Requires ``pywinrm``.  Install via the ``winrm`` extra::

    pip install testrange[winrm]
"""

from __future__ import annotations

import base64
import time

import winrm

from testrange.communication.base import AbstractCommunicator, ExecResult
from testrange.exceptions import VMTimeoutError, WinRMError

_POLL_INTERVAL = 3.0
"""Seconds between probe attempts in :meth:`WinRMCommunicator.wait_ready`."""

_FILE_CHUNK_SIZE = 128 * 1024
"""Bytes per chunked upload.  Kept conservative to stay below WinRM message
size defaults (``MaxEnvelopeSizekb``)."""


class WinRMCommunicator(AbstractCommunicator):
    """WinRM-backed communicator for Windows guests.

    :param host: Hostname or IP address of the Windows VM.
    :param username: Account to authenticate as (e.g. ``'Administrator'``).
    :param password: Plain-text password.
    :param port: TCP port (default ``5985`` for HTTP, ``5986`` for HTTPS).
    :param transport: WinRM auth transport: ``'ntlm'`` (default),
        ``'basic'``, ``'kerberos'``, ``'ssl'``, ``'credssp'``.
    :param scheme: ``'http'`` or ``'https'`` (default ``'http'``).
    """

    _endpoint: str
    """Composed ``<scheme>://<host>:<port>/wsman`` endpoint URL."""

    _username: str
    """Account used for all WinRM operations."""

    _password: str
    """Plain-text password (not hashed; WinRM handles auth)."""

    _transport: str
    """WinRM auth transport identifier passed to :class:`winrm.Session`."""

    # pywinrm's type stubs don't export ``Session`` on the top-level
    # module even though it's the public entry point everyone uses.
    # The runtime attribute exists; ignore the stub's lie.
    _session: winrm.Session | None  # pyright: ignore[reportAttributeAccessIssue]
    """Active pywinrm session; ``None`` until :meth:`wait_ready` succeeds."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 5985,
        transport: str = "ntlm",
        scheme: str = "http",
    ) -> None:
        self._endpoint = f"{scheme}://{host}:{port}/wsman"
        self._username = username
        self._password = password
        self._transport = transport
        self._session = None

    def _require_session(self) -> winrm.Session:  # pyright: ignore[reportAttributeAccessIssue]
        """Return the active session or raise if :meth:`wait_ready` never ran.

        :returns: The live :class:`winrm.Session`.
        :raises WinRMError: If no session has been established.
        """
        if self._session is None:
            raise WinRMError(
                "WinRM session not established — call wait_ready() first."
            )
        return self._session

    def wait_ready(self, timeout: int = 120) -> None:
        """Poll for a working WinRM endpoint until *timeout* expires.

        Sends a minimal ``$true`` PowerShell command; succeeds on the first
        response with ``status_code == 0``.

        :param timeout: Maximum seconds to wait.
        :raises VMTimeoutError: If WinRM stays unreachable after *timeout*.
        """
        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None

        while time.monotonic() < deadline:
            session = winrm.Session(  # pyright: ignore[reportAttributeAccessIssue]
                self._endpoint,
                auth=(self._username, self._password),
                transport=self._transport,
            )
            try:
                response = session.run_ps("$true")
                if response.status_code == 0:
                    self._session = session
                    return
                last_exc = WinRMError(
                    f"probe exited {response.status_code}: "
                    f"{response.std_err.decode(errors='replace').strip()}"
                )
            except Exception as exc:  # pywinrm raises several shapes
                last_exc = exc
            time.sleep(_POLL_INTERVAL)

        raise VMTimeoutError(
            f"WinRM at {self._endpoint} not ready after {timeout}s "
            f"(last error: {last_exc})"
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> ExecResult:
        """Execute *argv* on the Windows guest.

        When *env* is empty, the command runs via ``cmd.exe``; otherwise it
        runs via PowerShell with ``$env:`` assignments prepended.

        .. note::
            Per-call ``timeout`` is accepted for interface parity but not
            enforced — pywinrm's :class:`Session` does not expose a
            per-operation timeout.  Use the session-level
            ``operation_timeout_sec`` if fine-grained limits are needed.

        :param argv: Command and arguments.
        :param env: Extra environment variables.
        :param timeout: Accepted for interface parity; ignored.
        :returns: :class:`ExecResult` with exit code and raw output.
        :raises WinRMError: On protocol errors.
        """
        del timeout  # see docstring note
        session = self._require_session()
        cmd, *args = argv

        try:
            if env:
                env_prefix = "".join(
                    f'$env:{k}="{_ps_escape(v)}"; ' for k, v in env.items()
                )
                quoted_args = " ".join(f'"{_ps_escape(a)}"' for a in args)
                script = f"{env_prefix}& {cmd} {quoted_args}".rstrip()
                response = session.run_ps(script)
            else:
                response = session.run_cmd(cmd, args)
        except Exception as exc:
            raise WinRMError(f"WinRM exec failed for {argv!r}: {exc}") from exc

        return ExecResult(
            exit_code=response.status_code,
            stdout=response.std_out,
            stderr=response.std_err,
        )

    def get_file(self, path: str) -> bytes:
        """Read a remote file via PowerShell + base64.

        :param path: Absolute path inside the VM.
        :returns: Raw file contents.
        :raises WinRMError: On protocol errors or non-zero PowerShell exits.
        """
        session = self._require_session()
        script = (
            "[Convert]::ToBase64String("
            f"[System.IO.File]::ReadAllBytes('{_ps_escape(path)}'))"
        )
        try:
            response = session.run_ps(script)
        except Exception as exc:
            raise WinRMError(f"WinRM read of {path!r} failed: {exc}") from exc
        if response.status_code != 0:
            raise WinRMError(
                f"Remote read of {path!r} failed: "
                f"{response.std_err.decode(errors='replace').strip()}"
            )
        return base64.b64decode(response.std_out.strip())

    def put_file(self, path: str, data: bytes) -> None:
        """Upload *data* to *path* via chunked base64 PowerShell writes.

        The first chunk truncates the destination with ``WriteAllBytes``;
        subsequent chunks append via a file stream.

        :param path: Absolute remote destination.
        :param data: Bytes to write.
        :raises WinRMError: On protocol errors or non-zero PowerShell exits.
        """
        session = self._require_session()
        escaped = _ps_escape(path)

        # Ensure at least one iteration so empty writes still truncate the file.
        total = max(len(data), 1)
        for offset in range(0, total, _FILE_CHUNK_SIZE):
            chunk = data[offset : offset + _FILE_CHUNK_SIZE]
            b64 = base64.b64encode(chunk).decode()
            if offset == 0:
                script = (
                    "[System.IO.File]::WriteAllBytes("
                    f"'{escaped}', [Convert]::FromBase64String('{b64}'))"
                )
            else:
                script = (
                    f"$bytes = [Convert]::FromBase64String('{b64}'); "
                    f"$fs = [System.IO.File]::Open('{escaped}', 'Append'); "
                    "$fs.Write($bytes, 0, $bytes.Length); $fs.Close()"
                )
            try:
                response = session.run_ps(script)
            except Exception as exc:
                raise WinRMError(
                    f"WinRM write of {path!r} failed: {exc}"
                ) from exc
            if response.status_code != 0:
                raise WinRMError(
                    f"Remote write of {path!r} failed: "
                    f"{response.std_err.decode(errors='replace').strip()}"
                )

    def hostname(self) -> str:
        """Return the guest hostname via the ``hostname`` command.

        :returns: Hostname string with trailing newline stripped.
        :raises WinRMError: On protocol errors.
        """
        result = self.exec(["hostname"])
        return result.stdout.decode("utf-8", errors="replace").strip()


def _ps_escape(text: str) -> str:
    """Escape *text* for safe embedding in a single-quoted PowerShell string.

    :param text: Literal string to escape.
    :returns: Escaped string safe for ``'<text>'`` interpolation.
    """
    return text.replace("'", "''")
