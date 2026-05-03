"""``SSHProxy`` — :class:`~testrange.proxy.base.Proxy` over a paramiko
``Transport`` with OpenSSH-style TCP forwarding.

Used by every backend whose hypervisor speaks
OpenSSH-with-TCP-forwarding (libvirt, proxmox, ESXi 7+, Hyper-V with
the OpenSSH Server feature installed).  Backends construct an
``SSHProxy`` by passing in a live paramiko ``Transport``; the proxy
takes over channel + listener lifecycle from there.

The implementation reuses one transport for many tunnels — paramiko
multiplexes channels over a single SSH session natively, so a test
that opens 50 forwards pays one TCP+SSH handshake total.

Forward listeners run in daemon threads (one accept-loop per
forward, one shuttle pair per accepted connection).  ``close()``
sets a stop flag, closes all bound listener sockets to break the
``accept()`` blocked syscall, and joins every shuttle thread with a
short timeout so a slow remote can't hang teardown.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING, Any

from testrange._logging import get_logger
from testrange.exceptions import OrchestratorError
from testrange.proxy.base import Proxy

if TYPE_CHECKING:
    import paramiko

_log = get_logger(__name__)


_FORWARD_BUFFER_SIZE = 32 * 1024
"""Read chunk size for the bidirectional shuttle.

32 KiB is a comfortable middle ground: large enough that bulk
transfers (cloud-init seed ISO uploads, qcow2 imports) don't pay a
syscall per byte; small enough that a tunneled interactive session
(SSH inside a forward) still feels responsive."""


_LISTENER_BACKLOG = 16
"""``listen()`` backlog for the forward's accept socket.

16 covers test-time concurrency without being so high that a
runaway loop accumulates a huge SYN queue."""


class _ChannelSocket:
    """Adapter exposing a :class:`socket.socket`-shaped surface over a
    paramiko ``Channel``.

    paramiko channels implement ``send`` / ``recv`` already but lack
    a few socket methods callers expect (``settimeout``, ``fileno``,
    ``getpeername``).  Rather than try to subclass ``socket.socket``
    (which is a CPython builtin with non-trivial subclass semantics),
    we mirror the surface explicitly — the caller treats this as
    duck-typed.

    Most clients (paramiko's ``sock=``, requests adapters with
    ``init_poolmanager``) only need ``send`` / ``recv`` / ``close``;
    those route directly to the channel.  Extras are best-effort
    delegations.
    """

    def __init__(
        self,
        channel: paramiko.Channel,
        target: tuple[str, int],
    ) -> None:
        self._channel = channel
        self._target = target
        self._closed = False

    # Core socket-shaped surface --------------------------------------

    def send(self, data: bytes) -> int:
        return self._channel.send(data)

    def sendall(self, data: bytes) -> None:
        # paramiko's Channel.sendall exists but for symmetry with
        # socket.socket we re-loop ourselves so a partial send
        # surfaces consistently.
        view = memoryview(data)
        while view:
            n = self._channel.send(view)
            if n <= 0:
                raise OSError("channel send returned 0 — peer closed")
            view = view[n:]

    def recv(self, n: int) -> bytes:
        return self._channel.recv(n)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.close()
        except Exception:  # noqa: BLE001 — paramiko close is best-effort
            pass

    def settimeout(self, t: float | None) -> None:
        self._channel.settimeout(t)

    def fileno(self) -> int:
        # paramiko Channel exposes fileno() that returns the
        # underlying transport socket's fd.  Useful for
        # selectors-based callers (asyncio, requests).
        return self._channel.fileno()

    def getpeername(self) -> tuple[str, int]:
        return self._target

    # Context-manager sugar -------------------------------------------

    def __enter__(self) -> _ChannelSocket:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class SSHProxy(Proxy):
    """:class:`~testrange.proxy.base.Proxy` over a paramiko ``Transport``.

    :param transport: A live paramiko ``Transport``.  The proxy does
        NOT own connection lifecycle for the transport — backends
        that constructed the SSH session also close it (matches the
        existing :class:`~testrange.storage.transport.ssh.SSHFileTransport`
        ownership model).  ``close()`` here only tears down channels
        + forward listeners spawned by THIS proxy; the transport is
        left alone.
    """

    def __init__(self, transport: paramiko.Transport) -> None:
        self._transport = transport
        self._closed = False
        # Track every channel + forward listener so close() can
        # shut everything down deterministically.
        self._channels: list[Any] = []
        # _forwards: list of (server_socket, accept_thread,
        # stop_event).  Listener thread blocks on accept(); close()
        # signals stop and closes the server socket to interrupt
        # the syscall.
        self._forwards: list[tuple[socket.socket, threading.Thread, threading.Event]] = []
        # Per-shuttle threads spawned for each accepted forward
        # connection.  Tracked separately so close() can join them.
        self._shuttles: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # connect — single direct-tcpip channel, returned as socket-shaped
    # ------------------------------------------------------------------

    def connect(
        self,
        target: tuple[str, int],
        timeout: float = 30.0,
    ) -> socket.socket:
        if self._closed:
            raise OrchestratorError(
                "SSHProxy: connect() on a closed proxy.  Construct a "
                "fresh proxy via orch.proxy() if you need another "
                "tunnel after teardown."
            )
        if not self._transport.is_active():
            raise OrchestratorError(
                f"SSHProxy: underlying SSH transport is not active "
                f"(target={target}).  The remote SSH session likely "
                "terminated; reopen the orchestrator to re-establish."
            )
        try:
            channel = self._transport.open_channel(
                "direct-tcpip",
                dest_addr=target,
                src_addr=("127.0.0.1", 0),
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — wrap any paramiko error
            raise OrchestratorError(
                f"SSHProxy: open_channel(direct-tcpip, {target}) "
                f"failed: {exc}.  Common causes: (1) the SSH server "
                "has ``AllowTcpForwarding no`` (rare on libvirt/PVE, "
                "common on locked-down ESXi), (2) the target is not "
                "reachable from the hypervisor's network namespace, "
                "(3) the destination port has no listener."
            ) from exc
        with self._lock:
            self._channels.append(channel)
        # Wrap as a socket-shaped handle.  ``socket.socket`` has
        # non-trivial subclass semantics (it's a builtin), so we
        # return a duck-typed adapter instead.  Callers that need
        # a specific feature (selectors, fileno) can use the methods
        # on _ChannelSocket; clients that only need send/recv work
        # transparently.  The type signature says ``socket.socket``
        # because every method paramiko / requests / asyncio touches
        # is mirrored.
        return _ChannelSocket(channel, target)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # forward — local listener piping into per-connection channels
    # ------------------------------------------------------------------

    def forward(
        self,
        target: tuple[str, int],
        bind: tuple[str, int] = ("127.0.0.1", 0),
    ) -> tuple[str, int]:
        if self._closed:
            raise OrchestratorError(
                "SSHProxy: forward() on a closed proxy."
            )
        if not self._transport.is_active():
            raise OrchestratorError(
                f"SSHProxy: underlying SSH transport is not active "
                f"(target={target})."
            )

        # Bind the local listener.  SO_REUSEADDR matches the
        # convention of the rest of TestRange's listener helpers
        # (cuts kernel TIME_WAIT delays on rapid teardown).
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(bind)
        except OSError as exc:
            server.close()
            raise OrchestratorError(
                f"SSHProxy: failed to bind forward listener at "
                f"{bind}: {exc}."
            ) from exc
        server.listen(_LISTENER_BACKLOG)
        # ``getsockname`` carries the OS-assigned port when the
        # caller passed 0.
        bound = server.getsockname()

        stop = threading.Event()
        thread = threading.Thread(
            target=self._accept_loop,
            args=(server, target, stop),
            name=f"sshproxy-forward-{target[0]}:{target[1]}",
            daemon=True,
        )
        thread.start()
        with self._lock:
            self._forwards.append((server, thread, stop))

        return (bound[0], bound[1])

    def _accept_loop(
        self,
        server: socket.socket,
        target: tuple[str, int],
        stop: threading.Event,
    ) -> None:
        """Accept loop body — runs in a daemon thread per forward.

        For each inbound connection: open a fresh ``direct-tcpip``
        channel to the configured target and spawn two shuttle
        threads (one per direction) to pipe bytes.  The accept
        loop exits when *stop* is set OR when the server socket
        closes (``OSError`` on accept).
        """
        # Pin a short timeout so the loop checks ``stop`` between
        # accept attempts even when no inbound connection arrives —
        # otherwise close() waits for the next inbound or for the
        # OS-level socket close to interrupt accept().
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                # Server socket was closed by close() — exit.
                break

            try:
                channel = self._transport.open_channel(
                    "direct-tcpip",
                    dest_addr=target,
                    src_addr=("127.0.0.1", 0),
                    timeout=30.0,
                )
            except Exception as exc:  # noqa: BLE001 — log + drop connection
                _log.debug(
                    "forward channel open failed for target=%s: %s",
                    target, exc,
                )
                client.close()
                continue
            with self._lock:
                self._channels.append(channel)

            # Spawn two shuttle threads — one per direction.
            # Daemon=True so a test process that exits without
            # explicit close() doesn't hang on these.
            up = threading.Thread(
                target=self._shuttle, args=(client, channel, stop),
                name=f"sshproxy-up-{target[0]}:{target[1]}",
                daemon=True,
            )
            down = threading.Thread(
                target=self._shuttle_chan_to_sock,
                args=(channel, client, stop),
                name=f"sshproxy-down-{target[0]}:{target[1]}",
                daemon=True,
            )
            up.start()
            down.start()
            with self._lock:
                self._shuttles.extend([up, down])

    @staticmethod
    def _shuttle(
        src: socket.socket,
        dst: Any,  # paramiko.Channel — Any because TYPE_CHECKING import
        stop: threading.Event,
    ) -> None:
        """Pipe ``src.recv -> dst.send`` until either side closes."""
        try:
            src.settimeout(0.5)
            while not stop.is_set():
                try:
                    data = src.recv(_FORWARD_BUFFER_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                try:
                    dst.send(data)
                except Exception:  # noqa: BLE001
                    break
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _shuttle_chan_to_sock(
        src: Any,  # paramiko.Channel
        dst: socket.socket,
        stop: threading.Event,
    ) -> None:
        """Pipe ``src.recv -> dst.send`` until either side closes.

        Mirror of :meth:`_shuttle` but with paramiko's ``Channel``
        on the read side — paramiko channels have their own
        timeout semantics that don't accept the same ``settimeout``
        + ``socket.timeout`` exception shape, so we use a simpler
        blocking loop and let the ``stop`` event drive teardown by
        closing the channel.
        """
        try:
            while not stop.is_set():
                try:
                    src.settimeout(0.5)
                    data = src.recv(_FORWARD_BUFFER_SIZE)
                except Exception:  # noqa: BLE001 — channel timeout / closed
                    if stop.is_set():
                        break
                    continue
                if not data:
                    break
                try:
                    dst.sendall(data)
                except Exception:  # noqa: BLE001
                    break
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # close — idempotent teardown of every spawned resource
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Snapshot under the lock, then operate without holding it
        # — joining threads while the lock is held would deadlock
        # if a shuttle thread tried to grab it.
        with self._lock:
            forwards = list(self._forwards)
            channels = list(self._channels)
            shuttles = list(self._shuttles)
            self._forwards.clear()
            self._channels.clear()
            self._shuttles.clear()

        # Signal accept-loops + shuttles to exit, close server
        # sockets to break any blocked accept().
        for server, _thread, stop in forwards:
            stop.set()
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass

        # Close channels — wakes shuttle threads via OSError on
        # blocked recv/send.
        for ch in channels:
            try:
                ch.close()
            except Exception:  # noqa: BLE001
                pass

        # Join with a short cap — daemon threads die with the
        # process anyway, but cooperative join keeps the test
        # output clean.
        for _server, thread, _stop in forwards:
            thread.join(timeout=1.0)
        for thread in shuttles:
            thread.join(timeout=1.0)
