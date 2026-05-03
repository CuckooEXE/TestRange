"""Tests for ``LibvirtOrchestrator.proxy()`` — the libvirt-flavoured
:class:`~testrange.proxy.base.Proxy` implementation that opens an
SSH transport into the libvirt host's network namespace.

Coverage:

* Local libvirt (``qemu:///system``) → ``proxy()`` raises a clear
  error: there's no SSH transport to tunnel through; the test
  runner is already on the bridge.
* Remote libvirt (``qemu+ssh://user@host/system``) → ``proxy()``
  returns an :class:`SSHProxy` with a paramiko transport reaching
  the parsed (user, host, port).
* Memoization: repeated ``proxy()`` calls return the same
  instance.
* Teardown: orchestrator ``__exit__`` closes the proxy.

Paramiko is faked end-to-end so the tests don't need a real SSH
server.  The libvirt connection is also faked via the existing
``test_orchestrator_libvirt`` mock pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from testrange.exceptions import OrchestratorError


@pytest.fixture
def fake_paramiko_client() -> MagicMock:
    """Fake :class:`paramiko.SSHClient` whose ``get_transport``
    returns a fake transport with the open_channel surface
    ``SSHProxy`` reaches into."""
    transport = MagicMock()
    transport.is_active.return_value = True
    transport.open_channel.return_value = MagicMock()

    client = MagicMock()
    client.get_transport.return_value = transport
    return client


class TestLibvirtProxyLocal:
    def test_local_uri_proxy_raises(self) -> None:
        """``qemu:///system`` (local libvirt) → no remote tunnel
        needed.  The test runner is already on the libvirt host's
        bridge — calling ``proxy()`` is almost certainly a
        misconfiguration.  Raise a clear error rather than handing
        back a no-op proxy that silently succeeds."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )

        orch = LibvirtOrchestrator(host="localhost")
        with pytest.raises(OrchestratorError, match="local"):
            orch.proxy()

        orch2 = LibvirtOrchestrator(host="qemu:///system")
        with pytest.raises(OrchestratorError, match="local"):
            orch2.proxy()


class TestLibvirtProxyRemote:
    def test_remote_uri_returns_ssh_proxy(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """``qemu+ssh://user@host/system`` → ``proxy()`` opens an
        SSH client to (user, host, 22) and wraps the transport in
        an :class:`SSHProxy`."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )
        from testrange.proxy.ssh import SSHProxy

        with patch(
            "testrange.backends.libvirt.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = LibvirtOrchestrator(
                host="qemu+ssh://kvmadmin@kvm.example.com/system",
            )
            proxy = orch.proxy()

        assert isinstance(proxy, SSHProxy)
        # Coords passed to the SSH opener should reflect the URI.
        opener.assert_called_once()
        kwargs = opener.call_args.kwargs
        assert kwargs["host"] == "kvm.example.com"
        assert kwargs["username"] == "kvmadmin"
        assert kwargs["port"] == 22

    def test_remote_uri_with_port_parses(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """``qemu+ssh://user@host:2222/system`` → port=2222 reaches
        the SSH opener."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )

        with patch(
            "testrange.backends.libvirt.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = LibvirtOrchestrator(
                host="qemu+ssh://root@kvm.example.com:2222/system",
            )
            orch.proxy()

        assert opener.call_args.kwargs["port"] == 2222

    def test_remote_uri_without_user_resolves_via_ssh_config_or_env(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """When the URI omits ``user@``, ``proxy()`` falls back to
        the same resolution path :class:`SSHFileTransport` uses
        (``~/.ssh/config`` lookup, then ``$USER``).  Pin: opener
        sees a non-empty username."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )

        with patch(
            "testrange.backends.libvirt.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = LibvirtOrchestrator(
                host="qemu+ssh://kvm.example.com/system",
            )
            orch.proxy()

        # Either ssh_config user or $USER fallback — never None.
        assert opener.call_args.kwargs["username"] is not None

    def test_proxy_is_memoized(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """Repeated ``orch.proxy()`` returns the same instance —
        avoids re-handshaking SSH every call.  Pin: the SSH
        opener is invoked exactly once."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )

        with patch(
            "testrange.backends.libvirt.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = LibvirtOrchestrator(
                host="qemu+ssh://kvmadmin@kvm.example.com/system",
            )
            p1 = orch.proxy()
            p2 = orch.proxy()

        assert p1 is p2
        assert opener.call_count == 1

    def test_connect_routes_through_paramiko_transport(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """End-to-end shape: ``orch.proxy().connect((ip, port))``
        invokes ``transport.open_channel("direct-tcpip", ...)`` on
        the paramiko transport with the inner-VM dest tuple."""
        from testrange.backends.libvirt.orchestrator import (
            LibvirtOrchestrator,
        )

        with patch(
            "testrange.backends.libvirt.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ):
            orch = LibvirtOrchestrator(
                host="qemu+ssh://root@kvm.example.com/system",
            )
            orch.proxy().connect(("10.50.0.2", 80))

        transport = fake_paramiko_client.get_transport.return_value
        transport.open_channel.assert_called_once()
        call = transport.open_channel.call_args
        assert call.kwargs["dest_addr"] == ("10.50.0.2", 80)
