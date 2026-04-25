"""Proxmox surface-area tests.

These verify the *parts* of :mod:`testrange.backends.proxmox` that
are still pure scaffolding (``ProxmoxVM``, ``ProxmoxGuestAgentCommunicator``)
plus regression checks that the lazy ``proxmoxer`` import in the
orchestrator stays optional at module-load time.

End-to-end behaviour for the implemented pieces (orchestrator auth /
zone bootstrap, network start / stop) lives in
``tests/test_proxmox_live.py``, which only runs when a live PVE is
exposed via ``TESTRANGE_PROXMOX_HOST``.
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


class TestProxmoxOrchestratorOfflineSurface:
    """Offline checks: the orchestrator constructor stores its inputs
    without reaching out to a PVE host, and ``__enter__`` surfaces a
    clear error when called without credentials.  Live ``__enter__``
    behaviour against a real PVE is exercised in
    ``test_proxmox_live.py``."""

    def test_init_succeeds(self) -> None:
        from testrange.backends.proxmox import ProxmoxOrchestrator
        orch = ProxmoxOrchestrator(
            host="pve.example.com",
            node="pve01",
            storage="local-lvm",
        )
        assert orch.vms == {}
        assert orch._host == "pve.example.com"
        assert orch._node == "pve01"
        assert orch._storage == "local-lvm"
        assert orch._zone == "tr"
        assert orch._client is None  # not entered yet

    def test_enter_without_credentials_raises_clearly(self) -> None:
        """Constructor doesn't validate credentials (so callers can
        stash an instance for later); ``__enter__`` is where missing
        auth surfaces."""
        from testrange.backends.proxmox import ProxmoxOrchestrator
        from testrange.exceptions import OrchestratorError

        orch = ProxmoxOrchestrator(host="pve.example.com")
        with pytest.raises(OrchestratorError, match="no credentials"):
            orch.__enter__()


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


class TestProxmoxNetworkOfflineSurface:
    """Offline checks for :class:`ProxmoxVirtualNetwork`.  Live SDN
    create / delete behaviour is exercised in
    ``test_proxmox_live.py``."""

    def _net(self):
        from testrange.backends.proxmox import ProxmoxVirtualNetwork
        return ProxmoxVirtualNetwork("Net", "10.0.0.0/24")

    def test_backend_name_requires_bind_run(self) -> None:
        with pytest.raises(RuntimeError, match="bind_run"):
            self._net().backend_name()

    def test_backend_name_fits_pve_8_char_cap(self) -> None:
        """PVE rejects SDN IDs longer than 8 chars; the synthesis
        scheme must respect that for any reasonable input."""
        from testrange.backends.proxmox import ProxmoxVirtualNetwork
        cases = [
            ("Net", "abc-1234"),
            ("VeryLongLogicalNetworkName", "abcd1234efgh"),
            ("a", "0"),
            ("PUBLIC", "1234"),
        ]
        for name, run_id in cases:
            net = ProxmoxVirtualNetwork(name, "10.0.0.0/24")
            net.bind_run(run_id)
            backend_name = net.backend_name()
            assert len(backend_name) <= 8, (name, run_id, backend_name)
            # PVE only accepts ``[a-z0-9]`` in SDN IDs; verify the
            # sanitiser stripped everything else.
            assert backend_name.isalnum() and backend_name.islower(), (
                backend_name
            )

    def test_register_vm_returns_deterministic_mac(self) -> None:
        """Same scheme as the libvirt backend so a VM that lands on
        either backend gets the same MAC."""
        net = self._net()
        net.bind_run("abcd1234")
        mac1 = net.register_vm("web", "10.0.0.10")
        # Re-register a fresh net under the same logical name + run:
        # the deterministic scheme should produce the same MAC.
        net2 = self._net()
        net2.bind_run("ffff9999")  # different run
        mac2 = net2.register_vm("web", "10.0.0.10")
        assert mac1 == mac2  # MAC depends on (vm_name, net_name) only
        assert mac1.startswith("52:54:00:")  # QEMU OUI

    def test_stop_before_start_is_noop(self) -> None:
        """Best-effort teardown: never raises even if start was never
        called or only partially succeeded."""
        from unittest.mock import MagicMock
        net = self._net()
        net.bind_run("abcd1234")
        # No exception, no calls to the client.
        ctx = MagicMock()
        net.stop(ctx)
        ctx._client.cluster.sdn.vnets.assert_not_called()


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
