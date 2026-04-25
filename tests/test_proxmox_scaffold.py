"""Proxmox scaffolding surface-area tests.

These verify that:

1. Importing ``testrange.backends.proxmox`` succeeds on a machine
   without ``proxmoxer`` installed.  The scaffold is meant to be
   surveyed without extra dependencies.
2. Each Proxmox entry point raises :class:`NotImplementedError` with
   a "not yet implemented" message that points at the right place.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


def test_package_imports_without_proxmoxer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the scaffold must not ``import proxmoxer`` at
    module load time.  Drop proxmoxer out of sys.modules, clear the
    cached backends package, and re-import."""
    monkeypatch.setitem(sys.modules, "proxmoxer", None)  # poison
    for mod in list(sys.modules):
        if mod.startswith("testrange.backends.proxmox"):
            monkeypatch.delitem(sys.modules, mod)
    # Should not raise.
    import importlib
    importlib.import_module("testrange.backends.proxmox")


class TestProxmoxOrchestratorStubs:
    def test_init_succeeds(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            node="pve01",
            storage="local-lvm",
        )
        assert orch.vms == {}

    def test_enter_raises_with_message(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        orch = ProxmoxOrchestrator()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            orch.__enter__()

    def test_exit_raises_with_message(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        orch = ProxmoxOrchestrator()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            orch.__exit__(None, None, None)


class TestProxmoxVMStubs:
    def _vm(self):
        from testrange import Credential
        from testrange.backends.proxmox import ProxmoxVM
        return ProxmoxVM(
            name="x",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
        )

    def test_init_defers_to_libvirt_spec(self) -> None:
        vm = self._vm()
        assert vm.name == "x"

    def test_spec_attributes_populated(self) -> None:
        """Verify ``ProxmoxVM`` inherits the shared spec constructor —
        no libvirt wrapping under the hood, the standard attributes
        are populated directly on the instance."""
        from testrange.vms.builders import CloudInitBuilder
        vm = self._vm()
        assert vm.users[0].username == "root"
        assert isinstance(vm.builder, CloudInitBuilder)
        # Default communicator for a Linux qcow2 image is the
        # CloudInitBuilder default ("guest-agent" today).
        assert vm.communicator in {"guest-agent", "ssh", "winrm"}
        # ``_communicator`` is the slot the orchestrator writes to once
        # the VM is started; it's a plain instance attribute, not a
        # property forwarder onto a wrapped backend.
        assert vm._communicator is None
        sentinel = object()
        vm._communicator = sentinel  # type: ignore[assignment]
        assert vm._communicator is sentinel
        vm._communicator = None

    def test_does_not_wrap_libvirt(self) -> None:
        """Regression: ``ProxmoxVM`` must not carry a private
        ``_spec`` / libvirt VM instance.  The two backends are
        peers — both inherit :class:`AbstractVM` directly — so
        importing the proxmox backend should not require the libvirt
        backend's internals to construct a VM."""
        vm = self._vm()
        assert not hasattr(vm, "_spec")

    def test_build_raises(self) -> None:
        vm = self._vm()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            vm.build(
                context=MagicMock(),
                cache=MagicMock(),
                run=MagicMock(),
                install_network_name="",
                install_network_mac="",
            )

    def test_start_run_raises(self) -> None:
        vm = self._vm()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            vm.start_run(
                context=MagicMock(),
                run=MagicMock(),
                installed_disk=MagicMock(),
                network_entries=[],
                mac_ip_pairs=[],
            )

    def test_shutdown_raises(self) -> None:
        vm = self._vm()
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            vm.shutdown()


class TestProxmoxNetworkStubs:
    def _net(self):
        from testrange.backends.proxmox import ProxmoxVirtualNetwork
        return ProxmoxVirtualNetwork("Net", "10.0.0.0/24")

    def test_start_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._net().start(context=MagicMock())

    def test_stop_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._net().stop(context=MagicMock())

    def test_backend_name_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            self._net().backend_name()


class TestProxmoxGuestAgentStubs:
    """``ProxmoxGuestAgentCommunicator`` is the future REST-backed twin
    of the libvirt :class:`GuestAgentCommunicator`.  Until the Proxmox
    REST calls are implemented every method raises with a pointer at
    the upstream API endpoint that would satisfy it."""

    def _comm(self):
        from testrange.backends.proxmox import ProxmoxGuestAgentCommunicator
        return ProxmoxGuestAgentCommunicator(
            client=MagicMock(), node="pve01", vmid=100,
        )

    def test_init_stores_target(self) -> None:
        comm = self._comm()
        assert comm._node == "pve01"
        assert comm._vmid == 100

    def test_wait_ready_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._comm().wait_ready()

    def test_exec_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._comm().exec(["true"])

    def test_get_file_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._comm().get_file("/etc/hostname")

    def test_put_file_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._comm().put_file("/etc/hostname", b"x")

    def test_hostname_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="not yet implemented"):
            self._comm().hostname()
