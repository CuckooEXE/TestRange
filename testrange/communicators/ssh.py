"""SSHCommunicator — paramiko-backed SSH transport.

Plan-time::

    communicator=SSHCommunicator("myuser")

The orchestrator binds it with host + credential during the run phase,
then test code calls ``execute`` / ``read_file`` / ``write_file``.

The private key (if provided) is loaded from text in memory — never
written to the orchestrator host's filesystem.
"""

from __future__ import annotations

import io
import shlex
import socket
import time
from collections.abc import Sequence
from typing import Any

from testrange._log import get_logger
from testrange.communicators.base import Communicator, ExecResult
from testrange.credentials.posix import PosixCred
from testrange.exceptions import CommunicatorAlreadyBoundError, CommunicatorError

_log = get_logger(__name__)

# Connect retry policy: sshd typically takes 30-60s to come up after VM boot.
_CONNECT_TIMEOUT_PER_ATTEMPT_S = 10.0
_CONNECT_TOTAL_TIMEOUT_S = 180.0
_CONNECT_BACKOFF_S = 3.0


def _import_paramiko() -> Any:
    try:
        import paramiko
    except ImportError as e:
        raise CommunicatorError(
            "paramiko is not installed; install with `pip install -e .[ssh]`"
        ) from e
    return paramiko


def _load_private_key(text: str, paramiko_mod: Any) -> Any:
    """Try each key type until one parses.

    Paramiko 4.x dropped DSSKey; resolve class names lazily so we don't
    AttributeError on import-time-changing classes.
    """
    class_names = ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey")
    classes = tuple(
        cls for cls in (getattr(paramiko_mod, n, None) for n in class_names) if cls is not None
    )
    last_exc: Exception | None = None
    for cls in classes:
        try:
            sio = io.StringIO(text)
            return cls.from_private_key(sio)
        except paramiko_mod.SSHException as e:
            last_exc = e
            continue
    raise CommunicatorError(
        f"could not parse private key as any supported type: {last_exc}"
    ) from last_exc


class SSHCommunicator(Communicator):
    """SSH transport; binds at run-phase bring-up.

    Connection is lazy — the first ``execute``/``read_file``/``write_file``
    call opens it with a retry loop (sshd takes time after VM boot).
    """

    def __init__(self, username: str) -> None:
        if not isinstance(username, str) or not username:
            raise ValueError("SSHCommunicator(username) must be a non-empty string")
        self._username = username
        self._bound = False
        self._host: str | None = None
        self._port: int = 22
        self._credential: PosixCred | None = None
        self._client: Any | None = None

    @property
    def username(self) -> str:
        return self._username

    @property
    def is_bound(self) -> bool:
        return self._bound

    @property
    def host(self) -> str | None:
        return self._host

    def bind(self, *, host: str, credential: PosixCred, port: int = 22) -> None:
        """Bind to a live VM. Called by the orchestrator at run-phase bring-up."""
        if self._bound:
            raise CommunicatorAlreadyBoundError(
                f"SSHCommunicator({self._username!r}) already bound; "
                "construct a fresh instance per VM"
            )
        if not isinstance(host, str) or not host:
            raise ValueError("SSHCommunicator.bind(host=...) must be a non-empty string")
        if not isinstance(credential, PosixCred):
            raise TypeError(
                f"SSHCommunicator.bind(credential=...) must be a PosixCred, "
                f"got {type(credential).__name__}"
            )
        if credential.username != self._username:
            raise ValueError(
                f"credential.username={credential.username!r} does not match "
                f"SSHCommunicator username={self._username!r}"
            )
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"SSHCommunicator port must be 1..65535, got {port}")
        self._host = host
        self._port = port
        self._credential = credential
        self._bound = True

    def _ensure_connected(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._bound or self._host is None or self._credential is None:
            raise CommunicatorError(
                f"SSHCommunicator({self._username!r}) is not bound; "
                "the orchestrator must call .bind(host=, credential=) first"
            )
        paramiko = _import_paramiko()

        kwargs: dict[str, Any] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._credential.username,
            "timeout": _CONNECT_TIMEOUT_PER_ATTEMPT_S,
            "look_for_keys": False,
            "allow_agent": False,
        }
        # Auth precedence per PLAN.md decision 7: pkey if present, else password.
        if self._credential.privkey:
            kwargs["pkey"] = _load_private_key(self._credential.privkey, paramiko)
        elif self._credential.password:
            kwargs["password"] = self._credential.password
        else:
            raise CommunicatorError(
                f"PosixCred({self._username!r}) has neither pubkey/privkey nor password"
            )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        deadline = time.monotonic() + _CONNECT_TOTAL_TIMEOUT_S
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                client.connect(**kwargs)
                self._client = client
                _log.info("ssh connected to %s@%s:%d", self._username, self._host, self._port)
                return client
            except (paramiko.SSHException, OSError, socket.error) as e:  # noqa: UP024
                last_exc = e
                _log.debug("ssh connect retry: %s", e)
                time.sleep(_CONNECT_BACKOFF_S)
        raise CommunicatorError(
            f"SSH connect to {self._host}:{self._port} as {self._username} "
            f"failed after {_CONNECT_TOTAL_TIMEOUT_S:.0f}s: {last_exc}"
        )

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("execute(argv) requires a non-empty list")
        for a in argv:
            if not isinstance(a, str):
                raise TypeError(
                    f"execute(argv) entries must be str, got {type(a).__name__}"
                )
        client = self._ensure_connected()
        cmd = shlex.join(argv)
        if cwd:
            cmd = f"cd -- {shlex.quote(cwd)} && exec {cmd}"
        start = time.monotonic()
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdout_bytes = stdout.read()
        stderr_bytes = stderr.read()
        exit_code = stdout.channel.recv_exit_status()
        duration = time.monotonic() - start
        return ExecResult(
            exit_code=int(exit_code),
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            duration=duration,
        )

    def read_file(self, path: str) -> bytes:
        client = self._ensure_connected()
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "rb") as f:
                data: bytes = f.read()
                return data
        finally:
            sftp.close()

    def write_file(self, path: str, data: bytes) -> None:
        client = self._ensure_connected()
        sftp = client.open_sftp()
        try:
            with sftp.open(path, "wb") as f:
                f.write(data)
        finally:
            sftp.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                _log.warning("ssh close failed: %s", e)
            self._client = None
