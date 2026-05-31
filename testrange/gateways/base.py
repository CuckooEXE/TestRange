"""GuestGateway ABC — pluggable off-box reach to a guest TCP endpoint.

When the orchestrator runs off-box and a backend's guests sit on a network it
cannot route to (a remote hypervisor's isolated segment), the backend hands the
orchestrator a :class:`GuestGateway`. It brokers a connection to a guest's
``(host, port)`` without the consumer learning the mechanism behind it.

Two consumption shapes, because clients differ:

- :meth:`open_socket` hands back a **connected socket-like stream** — for a
  client that accepts a socket object (paramiko's ``sock=``). Cheap, with no
  extra local listener, so an SSH retry loop can call it per attempt.
- :meth:`open_local_forward` binds a **local 127.0.0.1 listener** that tunnels
  to ``(host, port)`` and returns its port — for a client that can only dial an
  address and cannot be given a socket (e.g. an HTTP client: it connects to
  ``localhost:<returned port>``).

The interface is deliberately technology-neutral so concretes slot in without
touching the communicator or the orchestrator: SSH ProxyJump is the first
(:class:`~testrange.gateways.ssh_jump.SSHJumpGateway`); a port-forward, a
WireGuard tunnel, a vsock bridge, etc. implement the same surface.

Mirrors the communicator pattern (``communicators/base.py``): an ABC with no
shared constructor — each concrete is configured with its own technology-specific
arguments and exposes the same minimal surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class GuestGateway(ABC):
    """Abstract broker that reaches a guest's ``(host, port)`` from off-box."""

    @abstractmethod
    def open_socket(self, host: str, port: int) -> Any:
        """Return a connected, socket-like stream to ``(host, port)``.

        For consumers that accept a socket object. The returned object must
        satisfy the subset of the stream-socket interface the consumer needs —
        for an SSH communicator, what paramiko's ``sock=`` expects
        (``send``/``recv``/``settimeout``/``fileno``/``close``/…); a paramiko
        ``Channel`` and a real ``socket.socket`` both qualify. Implementations
        **raise on failure** (the caller retries), distinguishing a non-retryable
        misconfiguration (:class:`~testrange.exceptions.GatewayError`) from a
        transient transport error (surfaced as the transport's own exception).
        """

    @abstractmethod
    def open_local_forward(self, host: str, port: int) -> int:
        """Bind a local ``127.0.0.1`` listener forwarding to ``(host, port)``.

        Returns the local port. For consumers that can only dial an address and
        cannot be handed a socket — they connect to ``127.0.0.1:<returned port>``
        and the gateway shuttles bytes to the guest endpoint. The forward stays
        up until :meth:`close`.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the underlying transport and any local forwards. Idempotent."""
