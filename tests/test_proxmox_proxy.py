"""Tests for ``ProxmoxOrchestrator.proxy()`` — the proxmox-flavoured
:class:`~testrange.proxy.base.Proxy` implementation.

The PVE node is the SSH endpoint for the tunnel: PVE owns the
SDN bridges and per-vnet dnsmasq, so any inner-VM IP is reachable
from the node's network namespace.  Two construction paths matter:

1. **External cluster**: operator pointed us at an existing PVE.  No
   SSH credential is implicit; ``proxy()`` raises with a clear
   pointer at the new ``ssh_user`` / ``ssh_key_filename`` ctor
   kwargs.
2. **Self-built node** (via ``root_on_vm``): the answer.toml-baked
   SSH key is on the hypervisor VM's communicator.  ``root_on_vm``
   plumbs that into the new ctor kwargs so ``proxy()`` works
   without further configuration.

Tests focus on the construction-time wiring + the raises-on-missing-
creds path.  End-to-end SSH-roundtrip coverage lives on the shared
:class:`SSHProxy` (``tests/test_proxy_ssh.py``); we don't re-prove
paramiko channel mechanics here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from testrange.exceptions import OrchestratorError


@pytest.fixture
def fake_paramiko_client() -> MagicMock:
    transport = MagicMock()
    transport.is_active.return_value = True
    transport.open_channel.return_value = MagicMock()
    client = MagicMock()
    client.get_transport.return_value = transport
    return client


class TestProxmoxProxyExternal:
    def test_proxy_raises_without_ssh_credentials(self) -> None:
        """External-cluster construction (no SSH creds) → ``proxy()``
        raises pointing at the missing kwargs.  Never silently
        return a no-op — that would let the caller proceed and fail
        later in a confusing way."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )

        orch = ProxmoxOrchestrator(host="pve.example.com")
        with pytest.raises(OrchestratorError, match="ssh"):
            orch.proxy()

    def test_proxy_raises_message_names_kwargs(self) -> None:
        """The error message names the new ctor kwargs so the
        operator can self-serve the fix."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )

        orch = ProxmoxOrchestrator(host="pve.example.com")
        with pytest.raises(OrchestratorError) as excinfo:
            orch.proxy()
        msg = str(excinfo.value)
        assert "ssh_user" in msg
        assert "ssh_key_filename" in msg or "ssh_password" in msg


class TestProxmoxProxyExplicitCreds:
    def test_proxy_with_ssh_user_and_key(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """Operator-supplied ``ssh_user`` + ``ssh_key_filename`` on
        the ctor → ``proxy()`` opens an SSH connection to the PVE
        node host with those creds and returns an :class:`SSHProxy`."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )
        from testrange.proxy.ssh import SSHProxy

        with patch(
            "testrange.backends.proxmox.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = ProxmoxOrchestrator(
                host="pve.example.com",
                ssh_user="root",
                ssh_key_filename="/tmp/key",
            )
            proxy = orch.proxy()

        assert isinstance(proxy, SSHProxy)
        opener.assert_called_once()
        kwargs = opener.call_args.kwargs
        assert kwargs["host"] == "pve.example.com"
        assert kwargs["username"] == "root"
        assert kwargs["key_filename"] == "/tmp/key"

    def test_proxy_with_ssh_password(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """``ssh_password`` (less-preferred but valid) flows through
        to the SSH opener as well."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )

        with patch(
            "testrange.backends.proxmox.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = ProxmoxOrchestrator(
                host="pve.example.com",
                ssh_user="root",
                ssh_password="hunter2",
            )
            orch.proxy()

        assert opener.call_args.kwargs["password"] == "hunter2"

    def test_proxy_default_port_22_overridable(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        """``ssh_port`` defaults to 22, can be overridden for hosts
        that run sshd on non-default ports (security hardening,
        port-knocking front-ends)."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )

        with patch(
            "testrange.backends.proxmox.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = ProxmoxOrchestrator(
                host="pve.example.com",
                ssh_user="root",
                ssh_key_filename="/tmp/key",
                ssh_port=2222,
            )
            orch.proxy()

        assert opener.call_args.kwargs["port"] == 2222

    def test_proxy_is_memoized(
        self, fake_paramiko_client: MagicMock
    ) -> None:
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )

        with patch(
            "testrange.backends.proxmox.orchestrator._open_ssh_for_proxy",
            return_value=fake_paramiko_client,
        ) as opener:
            orch = ProxmoxOrchestrator(
                host="pve.example.com",
                ssh_user="root", ssh_key_filename="/tmp/key",
            )
            p1 = orch.proxy()
            p2 = orch.proxy()

        assert p1 is p2
        assert opener.call_count == 1


class TestProxmoxProxyRootOnVm:
    def test_root_on_vm_plumbs_ssh_creds_into_orchestrator(self) -> None:
        """When ``root_on_vm`` builds the orchestrator, the resulting
        instance has ``_proxy_ssh_user`` + ``_proxy_ssh_key_filename``
        populated from the hypervisor's communicator's SSH key.
        Without this, ``orch.proxy()`` on a self-built node would
        raise the "no creds" error even though we have everything
        needed to authenticate."""
        from testrange.backends.proxmox.orchestrator import (
            ProxmoxOrchestrator,
        )
        from testrange.credentials import Credential

        hv = MagicMock()
        hv.name = "pve-hv"
        hv.users = [
            Credential("root", "rootpw", ssh_key="/path/to/key.pem"),
        ]
        hv.networks = []
        hv.vms = []
        comm = MagicMock()
        comm._host = "10.0.0.10"
        # SSHCommunicator stores a private key path on a known
        # attribute — root_on_vm reaches for it to populate the
        # proxy's SSH creds.  Pin the attribute name here so a
        # future communicator refactor can't silently regress.
        comm._key_filename = "/path/to/key.pem"
        comm._user = "root"
        hv._require_communicator.return_value = comm

        # Standard mock surface for the rest of root_on_vm.
        def _exec(argv, **_kw):  # type: ignore[no-untyped-def]
            r = MagicMock()
            r.exit_code = 0
            r.stderr = b""
            if argv[:2] == ["systemctl", "is-active"]:
                r.stdout = b"active\n"
            elif argv[0] == "sh" and "curl" in (argv[2] if len(argv) > 2 else ""):
                r.stdout = b"200"
            else:
                r.stdout = b""
            return r
        hv.exec.side_effect = _exec

        from pathlib import Path
        outer = MagicMock()
        outer._cache = MagicMock()
        outer._cache.root = Path("/tmp/cache")

        inner = ProxmoxOrchestrator.root_on_vm(hv, outer)

        # The constructed orchestrator carries the hypervisor's SSH
        # creds so its proxy() can authenticate without further
        # config.
        assert inner._proxy_ssh_user == "root"
        assert inner._proxy_ssh_key_filename == "/path/to/key.pem"
