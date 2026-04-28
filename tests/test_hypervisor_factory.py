"""Unit tests for the backend-neutral :func:`testrange.Hypervisor` factory.

The factory dispatches based on the ``orchestrator=`` kwarg to the
backend-native concrete hypervisor class —
:class:`LibvirtHypervisor` for libvirt orchestrators,
:class:`ProxmoxHypervisor` for Proxmox.  These tests cover the
dispatch table and the per-backend payload differences that motivated
the factory in the first place (libvirt has to inject libvirtd setup
packages; Proxmox doesn't because the PVE installer is the entire
install phase).
"""

from __future__ import annotations

import pytest

from testrange import (
    AbstractHypervisor,
    Credential,
    HardDrive,
    Hypervisor,
    LibvirtHypervisor,
    LibvirtOrchestrator,
    Memory,
    OrchestratorError,
    ProxmoxHypervisor,
    vNIC,
    vCPU,
)
from testrange.backends.proxmox import ProxmoxOrchestrator


def _common_kwargs() -> dict:
    """Hypervisor kwargs both backends accept identically."""
    return {
        "name": "hv",
        "iso": "https://example.com/img",
        "users": [Credential("root", "pw")],
        "devices": [
            vCPU(2),
            Memory(4),
            HardDrive(40),
            vNIC("OuterNet", ip="10.0.0.10"),
        ],
        "vms": [],
        "networks": [],
    }


class TestDispatchToLibvirt:
    def test_libvirt_orchestrator_yields_libvirt_hypervisor(self) -> None:
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **_common_kwargs())
        assert isinstance(hv, LibvirtHypervisor)
        assert isinstance(hv, AbstractHypervisor)
        assert hv.orchestrator is LibvirtOrchestrator

    def test_libvirt_path_injects_default_payload(self) -> None:
        # Regression guard: the libvirt Hypervisor pre-loads
        # libvirt-daemon-system + qemu-system-x86 + qemu-utils +
        # libvirt-clients and the systemctl-enable post-install hook.
        # The factory must not skip that injection.
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **_common_kwargs())
        pkg_names = {repr(p) for p in hv.pkgs}
        assert any("libvirt-daemon-system" in r for r in pkg_names)
        assert any("qemu-system-x86" in r for r in pkg_names)
        assert any(
            "systemctl enable --now libvirtd" in c
            for c in hv.post_install_cmds
        )


class TestDispatchToProxmox:
    def test_proxmox_orchestrator_yields_proxmox_hypervisor(self) -> None:
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        assert isinstance(hv, ProxmoxHypervisor)
        assert isinstance(hv, AbstractHypervisor)
        assert hv.orchestrator is ProxmoxOrchestrator

    def test_proxmox_path_injects_no_packages(self) -> None:
        # The PVE installer is the whole install phase — pveproxy /
        # pvedaemon ship with PVE itself, no apt step needed.  Anything
        # in ``pkgs`` is currently dead weight (PVE answer.toml ignores
        # it), so the factory must not silently inflate the cache key
        # by injecting libvirt-style payload packages.
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        assert hv.pkgs == []
        assert hv.post_install_cmds == []

    def test_proxmox_user_supplied_pkgs_pass_through(self) -> None:
        # Whatever the caller hands in, the factory forwards verbatim
        # — even if the current Proxmox builder doesn't render
        # ``pkgs=`` into answer.toml today, future first-boot support
        # would, and the field shouldn't be silently dropped here.
        from testrange.packages import Apt

        kwargs = _common_kwargs()
        kwargs["pkgs"] = [Apt("vim")]
        kwargs["post_install_cmds"] = ["echo hi"]
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **kwargs)
        assert any("vim" in repr(p) for p in hv.pkgs)
        assert "echo hi" in hv.post_install_cmds


class TestUnregisteredOrchestrator:
    def test_unknown_orchestrator_class_raises_clearly(self) -> None:
        # An orchestrator class no shipped backend recognises (third-
        # party in-progress backend, typo, etc.) must raise a
        # :class:`OrchestratorError` naming the class — not silently
        # return ``None`` or attempt to instantiate something
        # nonsensical.
        class HyperVOrch:
            """Stand-in for a not-yet-shipped backend."""

        with pytest.raises(OrchestratorError, match="HyperVOrch"):
            Hypervisor(orchestrator=HyperVOrch, **_common_kwargs())


class TestKwargForwarding:
    """The factory must not silently drop or rename kwargs — every
    field a backend's concrete ``__init__`` accepts has to make it
    through verbatim."""

    def test_inner_vms_and_networks_attached(self) -> None:
        from testrange import VM, VirtualNetwork

        inner_vm = VM(
            name="inner",
            iso="https://example.com/img",
            users=[Credential("root", "pw")],
            devices=[vNIC("InnerNet", ip="10.42.0.5")],
        )
        inner_net = VirtualNetwork("InnerNet", "10.42.0.0/24")

        for orch in (LibvirtOrchestrator, ProxmoxOrchestrator):
            hv = Hypervisor(
                orchestrator=orch,
                **{
                    **_common_kwargs(),
                    "vms": [inner_vm],
                    "networks": [inner_net],
                },
            )
            assert hv.vms == [inner_vm]
            assert hv.networks == [inner_net]
