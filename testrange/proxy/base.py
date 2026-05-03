"""``Proxy`` ABC — backend-supplied tunnel from the test runner into a
hypervisor's inner-network namespace.

Concrete subclasses implement :meth:`Proxy.connect` /
:meth:`Proxy.forward` against a backend-native transport (paramiko
over an SSH session for libvirt/proxmox/esxi-7+/hyperv-OpenSSH; PVE
termproxy, ESXi-pre-7 ProxyCommand, or Hyper-V WinRM ``netsh``
``portproxy`` for the fallbacks).
"""

from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from typing import Any


class Proxy(ABC):
    """Backend-supplied TCP tunnel from the test runner into the
    hypervisor's inner-VM network namespace.

    The orchestrator owns one ``Proxy`` per ``__enter__``'d session.
    Tunnels share the underlying transport (one paramiko ``Transport``
    per orchestrator, in the SSH-flavoured impl) and are torn down on
    the orchestrator's ``__exit__`` — the proxy is registered with
    the same :class:`~contextlib.ExitStack` that owns the inner
    orchestrator and the run scratch directory.

    Two complementary methods cover the spectrum of client shapes:

    * :meth:`connect` returns a connected :class:`socket.socket`-like
      handle for clients that accept a ``sock=`` (paramiko's
      :class:`paramiko.SSHClient`, :mod:`requests`'s
      :class:`HTTPSAdapter` with a custom ``init_poolmanager``,
      asyncio low-level).
    * :meth:`forward` returns a local ``(host, port)`` for opaque
      clients that only know how to connect to a ``host:port``
      string (``curl`` shelled out of the test, ``proxmoxer``, any
      third-party SDK with a hard-coded resolver path).

    Backend implementations choose the underlying tunnel mechanism:

    * **SSH-with-TCP-forwarding** (libvirt, proxmox, ESXi 7+,
      Hyper-V with Windows OpenSSH installed): paramiko
      ``Transport.open_channel("direct-tcpip", ...)``.  Implemented
      once in :class:`~testrange.proxy.ssh.SSHProxy`; backends just
      provide the ``Transport``.
    * **ProxyCommand fallback** (ESXi pre-7, where the stripped
      sshd lacks TCP-forwarding): ``ssh esxi-host nc target_ip
      target_port`` piped via paramiko's ``exec_command``.  Future
      ``ProxyCommandProxy`` subclass; not yet implemented.
    * **Windows ``netsh interface portproxy``** (Hyper-V hosts
      without OpenSSH Server): WinRM-driven ``netsh`` rule on the
      host.  Future ``WinRMPortProxy`` subclass; not yet
      implemented.

    Lifecycle:

    * Construct lazily — the orchestrator typically returns the
      same ``Proxy`` instance for repeated ``orch.proxy()`` calls
      (memoized, see backend impls).
    * Tunnels open lazily on the first ``connect`` / ``forward``;
      transport opens lazily on the first tunnel.
    * ``close()`` is idempotent and tears down every spawned
      forward listener thread + the transport itself.  After
      ``close()``, further ``connect`` / ``forward`` calls raise
      :class:`~testrange.exceptions.OrchestratorError`.
    """

    @abstractmethod
    def connect(
        self,
        target: tuple[str, int],
        timeout: float = 30.0,
    ) -> socket.socket:
        """Open a TCP connection to *target* via the backend's tunnel.

        Returns a connected ``socket.socket``-like handle the caller
        owns — closing the socket releases the channel; the
        underlying transport stays open for further calls.

        :param target: ``(host, port)`` tuple of the destination
            inside the hypervisor's network namespace.  ``host``
            may be an IP or a hostname the hypervisor can resolve
            (DNS resolution happens at the hypervisor side, not on
            the test runner — names like ``webserver.LabNet`` work
            when PVE's per-vnet dnsmasq registers them).
        :param timeout: Seconds to wait for the channel-open
            handshake.  Defaults to 30s, which covers slow SSH
            servers without masking a true hang.
        :returns: Connected socket-like handle.  Bytes written via
            ``send`` reach the target; bytes available on the
            target arrive via ``recv``.
        :raises OrchestratorError: If the proxy is already
            ``close()``d, the underlying transport is dead, or
            the SSH server refuses the channel
            (``administratively prohibited``).
        """

    @abstractmethod
    def forward(
        self,
        target: tuple[str, int],
        bind: tuple[str, int] = ("127.0.0.1", 0),
    ) -> tuple[str, int]:
        """Open a local listener that pipes to *target*.

        The listener accepts inbound connections on the test runner
        and shuttles bytes through a fresh tunnel channel per
        accepted connection.  Equivalent to ``ssh -L
        local_host:local_port:target_host:target_port hypervisor``,
        but in-process and with no shell-out.

        :param target: ``(host, port)`` of the inner endpoint.
            Same semantics as :meth:`connect`.
        :param bind: ``(host, port)`` for the local listener.
            Defaults to ``("127.0.0.1", 0)`` — loopback only,
            ephemeral port assigned by the OS.  Pass ``("0.0.0.0",
            ...)`` to share the forward with peer machines (rarely
            what you want; opt-in for shared-runner setups).
        :returns: The actual ``(host, port)`` the listener bound
            to.  When *bind* port was 0, this carries the
            OS-assigned ephemeral port the caller should aim
            clients at.
        :raises OrchestratorError: Same conditions as :meth:`connect`,
            plus failure to bind the local listener (already-in-use
            port, permission denied on a privileged port).
        """

    @abstractmethod
    def close(self) -> None:
        """Tear down all forwards and the underlying transport.

        Idempotent — may be called repeatedly without raising.  The
        orchestrator's ``__exit__`` calls this via ExitStack so
        test authors don't have to manage proxy lifetime manually,
        but explicit ``close()`` is supported for callers that want
        a tighter teardown ordering.

        After ``close()``, further ``connect`` / ``forward`` calls
        raise :class:`~testrange.exceptions.OrchestratorError`.
        """

    # Context-manager sugar so callers can ``with orch.proxy() as
    # pj: ...`` for tighter lifecycle control without wiring into
    # the orchestrator's ExitStack.

    def __enter__(self) -> Proxy:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
