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
``close`` is **not terminal**: a later deliberate :meth:`open_socket` /
:meth:`open_local_forward` lazily re-establishes a tracked jump (the
Communicator ``close()`` contract rides on this, PROXY-3) — only a stale
``_serve`` thread racing ``close()`` is refused (PROXY-2).
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
    # Guards _client/_listeners/_generation against _serve threads racing close().
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # Bumped by close(). A _serve thread carries the generation it was spawned
    # under and is refused once it goes stale (PROXY-2); deliberate calls pass
    # no generation and may lazily re-establish the jump (PROXY-3).
    _generation: int = field(default=0, init=False, repr=False)

    def _ensure_jump(self) -> Any:
        """Return the live bastion client, dialling lazily. Call with ``_lock`` held."""
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

    def _channel_to(
        self, host: str, port: int, origin: tuple[str, int], *, generation: int | None = None
    ) -> Any:
        with self._lock:
            if generation is not None and generation != self._generation:
                # A local-forward _serve thread can win the accept() race against
                # close() and dial here after teardown; reconnecting would
                # resurrect a brand-new, untracked bastion client that nothing
                # ever tears down (PROXY-2). Refuse — _serve catches this and
                # unwinds on its next accept(). A DELIBERATE post-close call
                # passes no generation and falls through to a lazy, *tracked*
                # reconnect instead: close() is not terminal (the Communicator
                # close() contract, PROXY-3).
                raise GatewayError(f"SSHJumpGateway({self.username}@{self.host}) is closed")
            client = self._ensure_jump()
        transport = client.get_transport()
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
        # The whole setup runs under the lock so close() can never slip between
        # the jump dial and the listener registration (an unregistered listener
        # would survive close() with its accept loop alive).
        with self._lock:
            self._ensure_jump()
            generation = self._generation
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self._listeners.append(listener)
        local_port: int = listener.getsockname()[1]
        # Daemon threads that unwind on their own when the sockets they own close
        # (the listener in close(), the channels when the jump transport drops),
        # so there is no stop-flag to manage.
        threading.Thread(
            target=self._serve, args=(listener, host, port, generation), daemon=True
        ).start()
        _log.info("ssh jump local-forward 127.0.0.1:%d -> %s:%d", local_port, host, port)
        return local_port

    def _serve(self, listener: socket.socket, host: str, port: int, generation: int) -> None:
        while True:
            try:
                conn, _peer = listener.accept()
            except OSError:
                return  # listener closed by close()
            try:
                chan = self._channel_to(host, port, conn.getpeername(), generation=generation)
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
        with self._lock:
            # Bump first (under the lock) so a _serve thread mid-dial goes stale
            # and is refused rather than reconnecting through _ensure_jump after
            # the teardown below (PROXY-2). Deliberate later opens re-establish.
            self._generation += 1
            for listener in self._listeners:
                listener.close()  # unblocks the accept() loop; pumps unwind on channel close
            self._listeners.clear()
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception as e:  # pragma: no cover - best-effort teardown
                _log.warning("ssh jump close failed: %s", e)
