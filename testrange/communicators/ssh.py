"""SSHCommunicator — paramiko-backed SSH transport.

Plan-time::

    communicator=SSHCommunicator("myuser")

The orchestrator binds it with host + credential during the run phase,
then test code calls ``execute`` / ``read_file`` / ``write_file``.

For a multi-NIC VM, pass ``nic_idx`` to choose which NIC's address to
connect on (by position in the VM's device list — the only thing that
disambiguates multiple NICs on one network). Omitted, the orchestrator
uses the first NIC that carries an address::

    communicator=SSHCommunicator("myuser", nic_idx=1)
"""

from __future__ import annotations

import contextlib
import io
import shlex
import socket
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.communicators.base import Communicator, ExecResult
from testrange.credentials.posix import PosixCred
from testrange.exceptions import CommunicatorAlreadyBoundError, CommunicatorError

if TYPE_CHECKING:
    from testrange.gateways.base import GuestGateway

_log = get_logger(__name__)


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

    def __init__(self, username: str, *, nic_idx: int | None = None) -> None:
        if not isinstance(username, str) or not username:
            raise ValueError("SSHCommunicator(username) must be a non-empty string")
        if nic_idx is not None:
            # bool is an int subclass; reject it so nic_idx=True isn't taken as 1.
            if isinstance(nic_idx, bool) or not isinstance(nic_idx, int):
                raise TypeError("SSHCommunicator(nic_idx) must be an int or None")
            if nic_idx < 0:
                raise ValueError(f"SSHCommunicator(nic_idx) must be >= 0, got {nic_idx}")
        self._username = username
        self._nic_idx = nic_idx
        self._bound = False
        self._host: str | None = None
        self._port: int = 22
        self._credential: PosixCred | None = None
        self._gateway: GuestGateway | None = None
        self._client: Any | None = None

    @property
    def username(self) -> str:
        return self._username

    @property
    def nic_idx(self) -> int | None:
        """Which NIC (by position in the VM's device list) the orchestrator
        should resolve the SSH address from. ``None`` => first addressed NIC."""
        return self._nic_idx

    @property
    def is_bound(self) -> bool:
        return self._bound

    @property
    def host(self) -> str | None:
        return self._host

    @property
    def gateway(self) -> GuestGateway | None:
        """The jump gateway this VM was bound through, or ``None`` if direct.

        Set when the backend's guests are not directly routable from the
        orchestrator (a remote hypervisor); ``None`` for a co-located backend.
        """
        return self._gateway

    def bind(
        self,
        *,
        host: str,
        credential: PosixCred,
        port: int = 22,
        gateway: GuestGateway | None = None,
    ) -> None:
        """Bind to a live VM. Called by the orchestrator at run-phase bring-up.

        ``gateway`` is an optional :class:`~testrange.gateways.base.GuestGateway`
        for backends whose guests are not directly routable from the orchestrator
        (a remote hypervisor): when set, the connection is tunnelled through it
        instead of dialled directly. ``host`` is always the guest's own address —
        the gateway, not the communicator, knows how to reach it.
        """
        if self._bound:
            raise CommunicatorAlreadyBoundError(
                f"SSHCommunicator({self._username!r}) already bound; "
                "construct a fresh instance per VM"
            )
        if not host:
            raise ValueError("SSHCommunicator.bind(host=...) must be non-empty")
        if credential.username != self._username:
            raise ValueError(
                f"credential.username={credential.username!r} does not match "
                f"SSHCommunicator username={self._username!r}"
            )
        if not (1 <= port <= 65535):
            raise ValueError(f"SSHCommunicator port must be 1..65535, got {port}")
        self._host = host
        self._port = port
        self._credential = credential
        self._gateway = gateway
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

        # sshd typically accepts connections only after cloud-init's
        # network.target completes — empirically 30-60s on debian-13 cloud
        # images with 2 vCPU / 1GB RAM. Cap total wait at 180s for slow
        # hosts; back off 3s between attempts so we don't hammer the port.
        per_attempt_timeout_s = 10.0
        total_timeout_s = 180.0
        backoff_s = 3.0

        kwargs: dict[str, Any] = {
            "hostname": self._host,
            "port": self._port,
            "username": self._credential.username,
            "timeout": per_attempt_timeout_s,
            "look_for_keys": False,
            "allow_agent": False,
        }
        # Auth precedence: pkey if present, else password.
        if self._credential.ssh_key:
            kwargs["pkey"] = _load_private_key(self._credential.ssh_key.priv, paramiko)
        elif self._credential.password:
            kwargs["password"] = self._credential.password
        else:
            raise CommunicatorError(
                f"PosixCred({self._username!r}) has neither ssh_key nor password"
            )

        client = paramiko.SSHClient()
        # Trust-on-first-use: every test VM is freshly provisioned with a new
        # host key, so there is no known_hosts entry to verify against and
        # AutoAddPolicy is the only thing that works here. This is safe ONLY
        # because the guests are ephemeral and on an isolated test range — do
        # not lift this pattern into production code, where it defeats MITM
        # protection.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        deadline = time.monotonic() + total_timeout_s
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            sock = None
            try:
                # When a gateway is bound, tunnel through it: open a fresh socket
                # to the guest each attempt. paramiko dials over the supplied sock,
                # but it only closes a sock it creates itself — a user-supplied one
                # (here, a live direct-tcpip channel on the bastion) survives a
                # failed connect() and would leak one channel per retry. Close it
                # ourselves on failure so a slow-booting guest can't exhaust the
                # bastion's channel window over many warmup attempts (COMM-7).
                if self._gateway is not None:
                    sock = self._gateway.open_socket(self._host, self._port)
                    kwargs["sock"] = sock
                client.connect(**kwargs)
                self._client = client
                _log.info("ssh connected to %s@%s:%d", self._username, self._host, self._port)
                return client
            except (paramiko.SSHException, OSError, socket.error) as e:  # noqa: UP024
                last_exc = e
                if sock is not None:
                    with contextlib.suppress(Exception):
                        sock.close()
                _log.debug("ssh connect retry: %s", e)
                time.sleep(backoff_s)
        raise CommunicatorError(
            f"SSH connect to {self._host}:{self._port} as {self._username} "
            f"failed after {total_timeout_s:.0f}s: {last_exc}"
        )

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        """Run ``argv`` over the SSH channel and return its :class:`ExecResult`.

        stdout and stderr share one channel, so they are drained *concurrently*
        (stderr on a helper thread): a command that floods one stream cannot fill
        its channel window and wedge the read of the other. ``timeout`` (set on
        the channel, and on the stderr join) bounds a stalled stream — a stall
        raises rather than hanging forever, surfaced as a :class:`CommunicatorError`
        here (paramiko's raw ``socket.timeout`` / ``SSHException`` would otherwise
        leak past the communicator boundary). **Not thread-safe across callers**:
        the cached paramiko client is shared, and TestRange is single-threaded /
        single-instance (ADR-0002, ADR-0018), so one communicator is driven by one
        caller; the stderr helper thread is internal to a single ``execute`` call.
        """
        if not argv:
            raise ValueError("execute(argv) requires a non-empty list")
        for a in argv:
            if not isinstance(a, str):
                raise TypeError(f"execute(argv) entries must be str, got {type(a).__name__}")
        client = self._ensure_connected()
        paramiko = _import_paramiko()
        cmd = shlex.join(argv)
        if cwd:
            cmd = f"cd -- {shlex.quote(cwd)} && exec {cmd}"
        start = time.monotonic()
        try:
            _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
            err_chunks: list[bytes] = []
            err_exc: list[BaseException] = []

            def _drain_stderr() -> None:
                try:
                    err_chunks.append(stderr.read())
                except BaseException as exc:  # re-raised on the main thread post-join
                    err_exc.append(exc)

            drain = threading.Thread(target=_drain_stderr, name="tr-ssh-stderr", daemon=True)
            drain.start()
            try:
                stdout_bytes = stdout.read()
                # paramiko's recv_exit_status() waits on an internal event with NO
                # timeout — it ignores the channel timeout entirely and blocks
                # forever if the peer sends EOF (so the read above returns) but
                # never delivers an exit-status request and keeps the channel
                # half-open. Bound it against the same `timeout` instead, and close
                # the channel on a miss so it surfaces as a CommunicatorError rather
                # than a silent hang (no build-phase watchdog covers run/test
                # execute() calls) — COMM-6.
                channel = stdout.channel
                exit_deadline = time.monotonic() + timeout
                while not channel.exit_status_ready():
                    if time.monotonic() >= exit_deadline:
                        with contextlib.suppress(Exception):
                            channel.close()
                        raise CommunicatorError(
                            f"SSH exec of {cmd!r} on {self._host} returned EOF but no "
                            f"exit status within {timeout:.0f}s (half-open channel)"
                        )
                    time.sleep(0.05)
                exit_code = channel.recv_exit_status()
            finally:
                drain.join(timeout)
            if drain.is_alive():
                # The drain thread is still blocked on stderr.read() against the
                # live channel. Close the channel so that read returns and the
                # daemon thread (and its fd) is released, instead of leaking until
                # the whole client is closed.
                with contextlib.suppress(Exception):
                    stdout.channel.close()
                raise CommunicatorError(
                    f"SSH stderr drain of {cmd!r} on {self._host} did not finish "
                    f"within {timeout:.0f}s"
                )
            if err_exc:
                raise err_exc[0]
            stderr_bytes = err_chunks[0] if err_chunks else b""
        except (TimeoutError, OSError, EOFError, paramiko.SSHException) as e:
            # socket.timeout is TimeoutError; OSError covers socket.error; the
            # rest are channel-level failures. Wrap them so callers see one
            # exception type (CommunicatorError), not paramiko internals.
            raise CommunicatorError(
                f"SSH exec of {cmd!r} on {self._host} failed or timed out after {timeout:.0f}s: {e}"
            ) from e
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
            # confirm=True stats the remote file after upload and raises if its
            # size doesn't match the source, so a truncated transfer fails loud
            # instead of silently leaving a short file.
            sftp.putfo(io.BytesIO(data), path, confirm=True)
        except OSError as e:
            raise CommunicatorError(
                f"SFTP write of {len(data)} bytes to {path!r} on {self._host} failed: {e}"
            ) from e
        finally:
            sftp.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:
                _log.warning("ssh close failed: %s", e)
            self._client = None
        if self._gateway is not None:
            self._gateway.close()
