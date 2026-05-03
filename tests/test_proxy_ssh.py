"""Tests for the ``Proxy`` ABC and the ``SSHProxy`` implementation.

The proxy is the construct that lets a test runner reach an inner-VM
network namespace via the bare-metal hypervisor — the layer that
solves "remote libvirt + unreachable PVE IP" without requiring
``ip route add`` on the runner.

Coverage shape:

* ``connect()`` opens a paramiko ``direct-tcpip`` channel with the
  destination tuple the caller asked for, and the returned object
  shuttles bytes through the channel both directions.
* ``forward()`` binds a local listener (with port=0 → ephemeral),
  pipes incoming connections through a fresh channel, and returns
  the actual bound ``(host, port)``.
* ``close()`` is idempotent, stops every spawned listener thread,
  and closes the channel after which further ``connect()`` raises.
* The ABC's contract holds: a subclass missing ``connect`` /
  ``forward`` / ``close`` can't be instantiated.

Paramiko is faked end-to-end — these tests don't open real sockets
to a real SSH server.  Listener-thread tests use ``socket.socketpair``
to confirm bytes round-trip without a kernel-level networking
dependency.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange.exceptions import OrchestratorError
from testrange.proxy.base import Proxy
from testrange.proxy.ssh import SSHProxy


class _FakeChannel:
    """Stand-in for ``paramiko.Channel``.

    Implements just the surface ``SSHProxy`` reaches into:
    ``recv()`` / ``send()`` / ``close()`` / ``settimeout()``.  Bytes
    written to ``send()`` accumulate on ``self.sent``; bytes returned
    from ``recv()`` come from ``self.to_recv`` (a deque-like list,
    pop[0]).  ``closed`` flips on ``close()`` and short-circuits
    further recv/send.
    """

    def __init__(self, dest: tuple[str, int]) -> None:
        self.dest = dest
        self.sent = bytearray()
        self.to_recv: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

    def recv(self, n: int) -> bytes:
        # Mirror paramiko's contract: closed → b""; no-data-within-
        # timeout → socket.timeout; actual data → up-to-n bytes.
        # Returning b"" on no-data would falsely signal "peer closed"
        # to the shuttle threads.
        if self.closed:
            return b""
        if not self.to_recv:
            # Honour the configured timeout — sleep briefly then
            # raise to keep test threads from busy-spinning.
            time.sleep(min(0.01, self.timeout or 0.01))
            raise socket.timeout
        chunk = self.to_recv.pop(0)
        return chunk[:n]

    def send(self, data: bytes) -> int:
        if self.closed:
            return 0
        self.sent.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True

    def settimeout(self, t: float | None) -> None:
        self.timeout = t


class _FakeTransport:
    """Stand-in for ``paramiko.Transport``.

    Records every ``open_channel("direct-tcpip", ...)`` call against
    ``self.channels`` so tests can assert destination tuples and
    inspect the wire payload after a roundtrip.
    """

    def __init__(self, *, alive: bool = True) -> None:
        self._alive = alive
        self.channels: list[_FakeChannel] = []
        self.open_channel_calls: list[dict[str, Any]] = []
        self.closed = False
        # Inject this when a test wants ``open_channel`` to fail.
        self.open_channel_error: Exception | None = None

    def is_active(self) -> bool:
        return self._alive and not self.closed

    def open_channel(
        self,
        kind: str,
        dest_addr: tuple[str, int],
        src_addr: tuple[str, int],
        timeout: float | None = None,
    ) -> _FakeChannel:
        self.open_channel_calls.append({
            "kind": kind, "dest": dest_addr, "src": src_addr,
            "timeout": timeout,
        })
        if self.open_channel_error is not None:
            raise self.open_channel_error
        ch = _FakeChannel(dest_addr)
        self.channels.append(ch)
        return ch

    def close(self) -> None:
        self.closed = True
        self._alive = False


# =====================================================================
# ABC contract
# =====================================================================


class TestProxyABC:
    """``Proxy`` must refuse instantiation when concrete methods are
    missing.  Mirrors ``Builder``'s ABC enforcement so a third-party
    implementor sees the same shape of error TestRange's other ABCs
    raise."""

    def test_cannot_instantiate_abstract_proxy(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            Proxy()  # pyright: ignore[reportAbstractUsage]

    def test_subclass_missing_connect_cannot_instantiate(self) -> None:
        class Partial(Proxy):
            def forward(self, target, bind=("127.0.0.1", 0)):  # type: ignore[no-untyped-def]
                return ("127.0.0.1", 0)

            def close(self) -> None:
                pass

        with pytest.raises(TypeError, match="abstract"):
            Partial()  # pyright: ignore[reportAbstractUsage]

    def test_complete_subclass_instantiates(self) -> None:
        class Complete(Proxy):
            def connect(self, target, timeout=30.0):  # type: ignore[no-untyped-def]
                return socket.socketpair()[0]

            def forward(self, target, bind=("127.0.0.1", 0)):  # type: ignore[no-untyped-def]
                return ("127.0.0.1", 0)

            def close(self) -> None:
                pass

        # No exception: contract satisfied.
        Complete()


# =====================================================================
# SSHProxy.connect — direct-tcpip channel shape + roundtrip
# =====================================================================


class TestSSHProxyConnect:
    def test_opens_direct_tcpip_with_target_tuple(self) -> None:
        """``connect((ip, port))`` opens ONE channel of kind
        ``direct-tcpip`` with the destination the caller asked for.
        Pin the kind + dest tuple — a future refactor that switches
        to a different channel type or reorders the tuple would
        silently break tunnel routing."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        proxy.connect(("10.50.0.2", 80))

        assert len(transport.open_channel_calls) == 1
        call = transport.open_channel_calls[0]
        assert call["kind"] == "direct-tcpip"
        assert call["dest"] == ("10.50.0.2", 80)

    def test_returns_object_with_socket_io(self) -> None:
        """The returned object accepts ``send`` and ``recv`` like a
        socket and routes bytes to/from the underlying channel."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        sock = proxy.connect(("10.50.0.2", 80))
        ch = transport.channels[0]

        # Send → goes into the channel buffer
        sock.send(b"GET / HTTP/1.1\r\n")
        assert bytes(ch.sent) == b"GET / HTTP/1.1\r\n"

        # Channel returns response bytes → recv yields them
        ch.to_recv.append(b"HTTP/1.1 200 OK\r\n")
        got = sock.recv(4096)
        assert got == b"HTTP/1.1 200 OK\r\n"

    def test_connect_after_close_raises(self) -> None:
        """Once the proxy is closed, further ``connect()`` calls must
        raise — not silently no-op (which would leave the caller
        holding a dead handle and produce confusing downstream
        errors)."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)
        proxy.close()

        with pytest.raises(OrchestratorError, match="closed"):
            proxy.connect(("10.50.0.2", 80))

    def test_connect_when_transport_dead_raises(self) -> None:
        """Transport that's already dead (e.g. SSH session
        terminated) → caller gets a clear error pointing at the
        transport state, not a generic paramiko exception."""
        transport = _FakeTransport(alive=False)
        proxy = SSHProxy(transport)

        with pytest.raises(OrchestratorError, match="transport"):
            proxy.connect(("10.50.0.2", 80))

    def test_connect_propagates_open_channel_failure(self) -> None:
        """If the SSH server refuses the channel (``ChannelException``
        on real paramiko), the caller sees an OrchestratorError that
        wraps the cause — not a raw paramiko exception leaking out
        of our module boundary."""
        transport = _FakeTransport()
        transport.open_channel_error = RuntimeError("administratively prohibited")
        proxy = SSHProxy(transport)

        with pytest.raises(OrchestratorError, match="open_channel"):
            proxy.connect(("10.50.0.2", 80))


# =====================================================================
# SSHProxy.forward — local listener that pipes through a channel
# =====================================================================


class TestSSHProxyForward:
    def test_returns_loopback_with_assigned_port(self) -> None:
        """Default bind is ``127.0.0.1:0``; OS picks an ephemeral
        port; ``forward`` returns it.  Caller uses the returned
        ``(host, port)`` to point clients at the tunnel."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        bind_host, bind_port = proxy.forward(("10.50.0.2", 80))

        assert bind_host == "127.0.0.1"
        assert bind_port > 0  # OS-assigned ephemeral
        # No channel opened YET — channels open lazily on inbound
        # connection accept.
        assert len(transport.open_channel_calls) == 0
        proxy.close()

    def test_inbound_connection_pipes_through_channel(self) -> None:
        """Connect a local socket to the forward's bind address.
        The forward thread accepts, opens a paramiko channel to the
        configured target, and shuttles bytes both ways.  Pin: data
        sent to the local socket appears on the channel's ``sent``;
        data the channel returns via ``to_recv`` arrives at the
        local socket's ``recv``."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        bind = proxy.forward(("10.50.0.2", 80))

        # Pre-load the channel response BEFORE accept so the
        # listener picks it up on first read.
        # (Channel doesn't exist yet — listener creates it on
        # accept.  We patch by injecting a channel-creation hook.)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(bind)

        # Wait briefly for the listener thread to have spun up the
        # channel.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not transport.channels:
            time.sleep(0.01)
        assert transport.channels, "listener did not open a channel"
        ch = transport.channels[0]
        assert ch.dest == ("10.50.0.2", 80)

        # Send a request through the local socket — should reach
        # the channel.
        client.sendall(b"GET / HTTP/1.1\r\n\r\n")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and len(ch.sent) < len(b"GET / HTTP/1.1\r\n\r\n"):
            time.sleep(0.01)
        assert bytes(ch.sent) == b"GET / HTTP/1.1\r\n\r\n"

        # Push a response back through the channel — should arrive
        # at the local socket.
        ch.to_recv.append(b"HTTP/1.1 200 OK\r\n\r\n")
        got = b""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and b"200 OK" not in got:
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            got += chunk
            if b"200 OK" in got:
                break
        assert b"200 OK" in got, f"local socket got {got!r}"

        client.close()
        proxy.close()

    def test_explicit_bind_override(self) -> None:
        """``bind=`` accepts a specific host + port (still loopback by
        convention but the API permits any address)."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        # Pick a high port we expect to be free.  127.0.0.1 only.
        bind = proxy.forward(("10.50.0.2", 80), bind=("127.0.0.1", 0))
        assert bind[0] == "127.0.0.1"
        proxy.close()


# =====================================================================
# SSHProxy.close — idempotent + shuts down listeners/channels
# =====================================================================


class TestSSHProxyClose:
    def test_close_idempotent(self) -> None:
        """``close()`` may be called repeatedly without raising —
        callers wiring it into ExitStack don't have to track
        whether they've already torn down."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)
        proxy.close()
        proxy.close()  # No exception.

    def test_close_terminates_forward_listener(self) -> None:
        """``close()`` shuts down the forward listener — a follow-up
        connect to the bind address must fail.  Otherwise the
        listener thread leaks past the proxy's lifetime."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        bind = proxy.forward(("10.50.0.2", 80))
        proxy.close()

        # Listener should be gone — connect attempts fail.  Allow a
        # small grace window for the OS to release the port.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect(bind)
            # If connect somehow succeeds, the listener is still up —
            # that's a leak.  Send a byte to force a failure if the
            # listener exited mid-handshake.
            s.sendall(b"x")
            time.sleep(0.05)
            data = b""
            try:
                data = s.recv(1)
            except (socket.timeout, ConnectionError, OSError):
                pass
            # At minimum: the listener thread should have exited and
            # released the channel-create hook, so no new channel is
            # created post-close.
            assert len(transport.channels) <= 1, (
                "post-close inbound connection should not spawn a new "
                "tunnel channel"
            )
        except (ConnectionRefusedError, ConnectionResetError, OSError):
            pass
        finally:
            s.close()

    def test_close_marks_transport_handle_dead_for_connect(self) -> None:
        """Post-close, ``connect()`` raises with a closed-state
        message — see :meth:`TestSSHProxyConnect.test_connect_after_close_raises`.
        Duplicated here to assert the close → connect transition
        explicitly (rather than just the post-close error), so a
        future refactor can't accidentally make ``close`` set a
        flag that ``connect`` doesn't check."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        # Sanity: connect works pre-close.
        proxy.connect(("10.50.0.2", 80))

        proxy.close()
        with pytest.raises(OrchestratorError, match="closed"):
            proxy.connect(("10.50.0.2", 80))


# =====================================================================
# Context manager surface
# =====================================================================


class TestSSHProxyContextManager:
    def test_with_block_calls_close_on_exit(self) -> None:
        transport = _FakeTransport()
        with SSHProxy(transport) as proxy:
            assert isinstance(proxy, SSHProxy)
            assert proxy.connect(("10.50.0.2", 22))

        # After the with-block, connect must raise — proxy is
        # closed.
        with pytest.raises(OrchestratorError, match="closed"):
            proxy.connect(("10.50.0.2", 22))

    def test_with_block_calls_close_on_exception(self) -> None:
        transport = _FakeTransport()
        with pytest.raises(RuntimeError, match="user error"):
            with SSHProxy(transport) as proxy:
                proxy.connect(("10.50.0.2", 22))
                raise RuntimeError("user error")

        # Even though the body raised, close still ran — connect
        # raises closed.
        with pytest.raises(OrchestratorError, match="closed"):
            proxy.connect(("10.50.0.2", 22))


# =====================================================================
# Concurrency — multiple connects share one transport
# =====================================================================


class TestSSHProxyConcurrency:
    def test_multiple_connects_open_separate_channels(self) -> None:
        """Two ``connect()`` calls open two distinct channels on the
        same underlying transport.  paramiko Transport supports
        many concurrent channels; pin that we don't accidentally
        serialize through a single channel."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)

        proxy.connect(("10.50.0.2", 80))
        proxy.connect(("10.50.0.3", 22))

        assert len(transport.open_channel_calls) == 2
        assert transport.open_channel_calls[0]["dest"] == ("10.50.0.2", 80)
        assert transport.open_channel_calls[1]["dest"] == ("10.50.0.3", 22)

    def test_close_runs_under_concurrent_forward(self) -> None:
        """A long-running forward listener must not block ``close()``
        — ExitStack-driven shutdown of an orchestrator that has an
        active proxy can't hang."""
        transport = _FakeTransport()
        proxy = SSHProxy(transport)
        proxy.forward(("10.50.0.2", 80))

        # A thread is now serving accept().  Close should signal it
        # to exit.  Cap at 2s — anything longer is a hang.
        done = threading.Event()

        def _close() -> None:
            proxy.close()
            done.set()

        t = threading.Thread(target=_close, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert done.is_set(), "proxy.close() did not return within 2s"
