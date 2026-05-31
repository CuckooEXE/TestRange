"""SSHJumpGateway — reach a guest by tunnelling through an SSH bastion.

A generic SSH ProxyJump: connect to a reachable jump host, then reach a guest's
``(host, port)`` from there. It knows nothing of any specific backend — it is
configured with a plain SSH endpoint (host/port/user + password or private-key
text), so a remote driver whose guests are only reachable *via* its hypervisor
builds one from its own connection config and the orchestrator slots it into a
consumer unchanged.

Two reach shapes (see :class:`~testrange.gateways.base.GuestGateway`):

- :meth:`open_socket` opens a ``direct-tcpip`` channel and hands it back as a
  socket — no local listener, so it is cheap to call in a retry loop.
- :meth:`open_local_forward` binds a local listener and pumps each accepted
  connection over a fresh ``direct-tcpip`` channel — for clients that can only
  dial ``localhost:<port>``.

The jump connection is established lazily on first use and multiplexes every
channel opened afterwards; :meth:`close` stops the forwards and tears it down.
"""

from __future__ import annotations

import io
import select
import socket
import threading
from dataclasses import dataclass, field
from typing import Any

from testrange._log import get_logger
from testrange.exceptions import GatewayError
from testrange.gateways.base import GuestGateway

_log = get_logger(__name__)


def _import_paramiko() -> Any:
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover - exercised via the install-hint path
        raise GatewayError("paramiko is not installed; install with `pip install -e .[ssh]`") from e
    return paramiko


def _load_private_key(text: str, paramiko_mod: Any) -> Any:
    """Parse private-key text, trying each supported key type (paramiko 4.x-safe)."""
    class_names = ("Ed25519Key", "RSAKey", "ECDSAKey", "DSSKey")
    classes = tuple(
        cls for cls in (getattr(paramiko_mod, n, None) for n in class_names) if cls is not None
    )
    last_exc: Exception | None = None
    for cls in classes:
        try:
            return cls.from_private_key(io.StringIO(text))
        except paramiko_mod.SSHException as e:
            last_exc = e
    raise GatewayError(f"could not parse jump private key as any supported type: {last_exc}")


@dataclass
class SSHJumpGateway(GuestGateway):
    """Reach a guest by tunnelling through an SSH bastion.

    Configured with the bastion's SSH endpoint only; the guest target is supplied
    per call. Authenticates with ``pkey_text`` if present, else ``password``.
    """

    host: str
    username: str
    password: str | None = None
    pkey_text: str | None = None
    port: int = 22
    _client: Any = field(default=None, init=False, repr=False)
    _listeners: list[socket.socket] = field(default_factory=list, init=False, repr=False)

    def _ensure_jump(self) -> Any:
        if self._client is not None:
            return self._client
        paramiko = _import_paramiko()
        client = paramiko.SSHClient()
        # Ephemeral test bastion; same trust-on-first-use rationale as the
        # communicator — never lift into production where it defeats MITM checks.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": 10.0,
        }
        if self.pkey_text:
            kwargs["pkey"] = _load_private_key(self.pkey_text, paramiko)
        elif self.password:
            kwargs["password"] = self.password
        else:
            raise GatewayError(
                f"SSHJumpGateway({self.username}@{self.host}) has neither password nor pkey_text"
            )
        client.connect(**kwargs)
        self._client = client
        _log.info("ssh jump established via %s@%s:%d", self.username, self.host, self.port)
        return client

    def _channel_to(self, host: str, port: int, origin: tuple[str, int]) -> Any:
        transport = self._ensure_jump().get_transport()
        if transport is None:  # pragma: no cover - paramiko sets a transport on connect
            raise GatewayError(f"ssh jump to {self.host} has no live transport")
        return transport.open_channel("direct-tcpip", (host, port), origin)

    def open_socket(self, host: str, port: int) -> Any:
        """Open a fresh ``direct-tcpip`` channel from the bastion to ``(host, port)``.

        Lets paramiko's transport exceptions (``SSHException`` incl.
        ``ChannelException``, ``OSError``) propagate so a caller's retry loop can
        wait out a guest whose ``sshd`` is not up yet; only configuration faults
        raise :class:`~testrange.exceptions.GatewayError`.
        """
        return self._channel_to(host, port, ("127.0.0.1", 0))

    def open_local_forward(self, host: str, port: int) -> int:
        """Bind a local listener that tunnels each connection to ``(host, port)``."""
        self._ensure_jump()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        local_port: int = listener.getsockname()[1]
        # Daemon threads that unwind on their own when the sockets they own close
        # (the listener in close(), the channels when the jump transport drops),
        # so there is no stop-flag to manage.
        threading.Thread(target=self._serve, args=(listener, host, port), daemon=True).start()
        self._listeners.append(listener)
        _log.info("ssh jump local-forward 127.0.0.1:%d -> %s:%d", local_port, host, port)
        return local_port

    def _serve(self, listener: socket.socket, host: str, port: int) -> None:
        while True:
            try:
                conn, _peer = listener.accept()
            except OSError:
                return  # listener closed by close()
            try:
                chan = self._channel_to(host, port, conn.getpeername())
            except Exception as e:
                _log.debug("forward dial to %s:%d failed: %s", host, port, e)
                conn.close()
                continue
            threading.Thread(target=self._pump, args=(conn, chan), daemon=True).start()

    @staticmethod
    def _pump(left: Any, right: Any) -> None:
        """Shuttle bytes both ways between two stream sockets until either closes."""
        try:
            while True:
                readable, _, _ = select.select([left, right], [], [])
                for src, dst in ((left, right), (right, left)):
                    if src in readable:
                        data = src.recv(65536)
                        if not data:
                            return
                        dst.sendall(data)
        except OSError:
            return
        finally:
            left.close()
            right.close()

    def close(self) -> None:
        for listener in self._listeners:
            listener.close()  # unblocks the accept() loop; pumps unwind on channel close
        self._listeners.clear()
        if self._client is not None:
            try:
                self._client.close()
            except Exception as e:  # pragma: no cover - best-effort teardown
                _log.warning("ssh jump close failed: %s", e)
            self._client = None
