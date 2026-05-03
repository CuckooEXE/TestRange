"""Unit tests for the generic backend-neutral
:class:`testrange.Hypervisor` and the
:meth:`AbstractOrchestrator.prepare_outer_vm` payload-injection hook
it delegates to.

The class structure is two-axis:

- The Hypervisor itself is a :class:`GenericVM` + the three
  :class:`AbstractHypervisor` data fields.
- The inner orchestrator class declares its outer-VM payload via
  :meth:`prepare_outer_vm`.
- At outer-orchestrator construction, ``_promote_to_<backend>``
  translates the generic spec into the backend's concrete
  ``Hypervisor(BackendVM, AbstractHypervisor)`` so the
  provisioning-pipeline lifecycle methods (``_memory_kib``, ``build``,
  …) exist when called.

These tests exercise all four cross-product cases — outer × inner ∈
{libvirt, proxmox} — plus the ordering / forwarding details that
broke in earlier iterations.
"""

from __future__ import annotations

import pytest

from testrange import (
    AbstractHypervisor,
    Apt,
    Credential,
    HardDrive,
    Hypervisor,
    LibvirtOrchestrator,
    LibvirtVM,
    Memory,
    OrchestratorError,
    VirtualNetwork,
    vNIC,
    vCPU,
)
from testrange.backends.libvirt.orchestrator import _promote_to_libvirt
from testrange.backends.libvirt.hypervisor import (
    Hypervisor as LibvirtConcreteHypervisor,
)
from testrange.backends.proxmox import ProxmoxOrchestrator, ProxmoxVM
from testrange.backends.proxmox.hypervisor import (
    Hypervisor as ProxmoxConcreteHypervisor,
)
from testrange.backends.proxmox.orchestrator import _promote_to_proxmox

SSH_PUB = "ssh-ed25519 AAAAtest test@host"


def _common_kwargs() -> dict:
    return {
        "name": "hv",
        "iso": "https://example.com/img",
        "users": [Credential("root", "pw", ssh_key=SSH_PUB)],
        "devices": [
            vCPU(2),
            Memory(4),
            HardDrive(40),
            vNIC("OuterNet", ip="10.0.0.10"),
        ],
        "vms": [],
        "networks": [],
    }


class TestPayloadInjection:
    """The inner orchestrator class — not the Hypervisor itself —
    decides what software the outer VM needs."""

    def test_libvirt_inner_injects_libvirtd_payload(self) -> None:
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **_common_kwargs())
        pkg_names = [p.name for p in hv.pkgs]
        assert "libvirt-daemon-system" in pkg_names
        assert "qemu-system-x86" in pkg_names
        assert any(
            "systemctl enable --now libvirtd" in c
            for c in hv.post_install_cmds
        )

    def test_proxmox_inner_injects_nothing(self) -> None:
        # PVE installer is the whole install phase — pveproxy and
        # friends ship with the install ISO.  ``dnsmasq`` (needed
        # for the SDN integration the inner orch relies on) is
        # NOT injected via ``prepare_outer_vm`` because that would
        # rebake the whole qcow2 cache whenever the bootstrap script
        # changed; instead it's installed over SSH from
        # ``ProxmoxAnswerBuilder.post_install_hook`` during the
        # install phase, between cloud-init SHUTOFF and template
        # promotion.  Cache hash stays clean: empty pkgs /
        # post_install_cmds (the hook script's digest is folded
        # in via ``post_install_cache_key_extra`` instead).
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        assert hv.pkgs == []
        assert hv.post_install_cmds == []

    def test_caller_pkgs_run_after_payload(self) -> None:
        # Caller's commands typically depend on libvirtd already being
        # up — payload first, caller after.  Same ordering the
        # original libvirt-only Hypervisor used, preserved through
        # the refactor.
        kwargs = _common_kwargs()
        kwargs["pkgs"] = [Apt("tmux")]
        kwargs["post_install_cmds"] = ["echo hello"]
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **kwargs)
        pkg_names = [p.name for p in hv.pkgs]
        assert pkg_names[0] == "libvirt-daemon-system"
        assert pkg_names[-1] == "tmux"
        assert "systemctl enable --now libvirtd" in hv.post_install_cmds[0]
        assert hv.post_install_cmds[-1] == "echo hello"


class TestInnerNameValidation:
    """Backend-agnostic duplicate-name checks fire at Hypervisor
    construction.  Backend-specific stricter rules (libvirt's 10-char
    truncation, etc.) run again at promote time."""

    def _vm(self, name: str) -> LibvirtVM:
        return LibvirtVM(
            name=name,
            iso="https://example.com/img",
            users=[Credential("root", "pw")],
            devices=[vNIC("Inner", ip="10.42.0.5")],
        )

    def test_duplicate_inner_vm_name_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="duplicate VM name 'inner'"):
            Hypervisor(
                orchestrator=LibvirtOrchestrator,
                **{
                    **_common_kwargs(),
                    "vms": [self._vm("inner"), self._vm("inner")],
                },
            )

    def test_duplicate_inner_network_name_raises(self) -> None:
        with pytest.raises(OrchestratorError, match="duplicate network name"):
            Hypervisor(
                orchestrator=LibvirtOrchestrator,
                **{
                    **_common_kwargs(),
                    "networks": [
                        VirtualNetwork("X", "10.42.0.0/24"),
                        VirtualNetwork("X", "10.43.0.0/24"),
                    ],
                },
            )


class TestPromotionByOuterOrchestrator:
    """The outer orchestrator's ``_promote_to_<backend>`` translates a
    generic Hypervisor into the backend-flavoured concrete
    Hypervisor that has the lifecycle methods the provisioning
    pipeline calls."""

    def test_libvirt_outer_libvirt_inner(self) -> None:
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **_common_kwargs())
        promoted = _promote_to_libvirt(hv)
        assert isinstance(promoted, LibvirtConcreteHypervisor)
        assert isinstance(promoted, LibvirtVM)
        assert isinstance(promoted, AbstractHypervisor)
        # Lifecycle helpers the libvirt provisioning code calls.
        assert promoted._memory_kib() == 4 * 1024 * 1024
        # Hypervisor data fields preserved verbatim.
        assert promoted.orchestrator is LibvirtOrchestrator

    def test_libvirt_outer_proxmox_inner(self) -> None:
        # The bug case my earlier factory broke: outer libvirt with
        # an inner-Proxmox Hypervisor.  Must promote to a libvirt-
        # shaped concrete (so libvirt provisioning works) but carry
        # the Proxmox orchestrator class as the inner pointer.
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        promoted = _promote_to_libvirt(hv)
        assert isinstance(promoted, LibvirtConcreteHypervisor)
        assert isinstance(promoted, LibvirtVM)
        assert promoted.orchestrator is ProxmoxOrchestrator
        # No package injection — ProxmoxOrchestrator doesn't override
        # ``prepare_outer_vm`` (the dnsmasq the SDN integration needs
        # is installed via the SSH bootstrap that
        # ``ProxmoxAnswerBuilder.post_install_hook`` runs in the
        # install phase between SHUTOFF and template promotion, NOT
        # via the install-time package list).
        assert promoted.pkgs == []

    def test_proxmox_outer_libvirt_inner(self) -> None:
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **_common_kwargs())
        promoted = _promote_to_proxmox(hv)
        assert isinstance(promoted, ProxmoxConcreteHypervisor)
        assert isinstance(promoted, ProxmoxVM)
        assert promoted.orchestrator is LibvirtOrchestrator
        # libvirtd payload survived the cross-backend hop.
        assert any(p.name == "libvirt-daemon-system" for p in promoted.pkgs)
        # Proxmox-shaped accessor exists.
        assert promoted._memory_mib() == 4 * 1024

    def test_proxmox_outer_proxmox_inner(self) -> None:
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        promoted = _promote_to_proxmox(hv)
        assert isinstance(promoted, ProxmoxConcreteHypervisor)
        assert isinstance(promoted, ProxmoxVM)
        assert promoted.orchestrator is ProxmoxOrchestrator
        # No package injection — ProxmoxOrchestrator doesn't override
        # ``prepare_outer_vm``; bootstrap happens over SSH after boot.
        assert promoted.pkgs == []


class TestPromoteIdempotence:
    def test_already_libvirt_hypervisor_passes_through(self) -> None:
        hv = LibvirtConcreteHypervisor(
            orchestrator=LibvirtOrchestrator, **_common_kwargs()
        )
        assert _promote_to_libvirt(hv) is hv

    def test_already_proxmox_hypervisor_passes_through(self) -> None:
        hv = ProxmoxConcreteHypervisor(
            orchestrator=ProxmoxOrchestrator, **_common_kwargs()
        )
        assert _promote_to_proxmox(hv) is hv


class TestPrepareOuterVmDefault:
    def test_default_is_no_op(self) -> None:
        # Any orchestrator class that doesn't override prepare_outer_vm
        # leaves the Hypervisor's spec untouched.  Verifies the
        # default landing for backends whose installer ISOs are
        # self-contained.
        from testrange.orchestrator_base import AbstractOrchestrator

        class _StubOrch(AbstractOrchestrator):
            def __enter__(self): raise NotImplementedError
            def __exit__(self, *a): raise NotImplementedError

        hv = Hypervisor(orchestrator=_StubOrch, **_common_kwargs())
        assert hv.pkgs == []
        assert hv.post_install_cmds == []


class TestPromoteFieldParity:
    """The ``_promote_to_*`` functions in the libvirt and proxmox
    orchestrators copy generic-spec fields one-by-one into the
    backend-native concrete classes.  These field lists are
    duplicated across backends — easy to forget to update when a
    new field lands.  The tests below catch the case where a generic
    spec carries a field the promote function silently drops.

    They don't replace adding the field to the promote function;
    they just make the lapse loud at test time instead of "VM
    works in libvirt but the same spec breaks on Proxmox."
    """

    def test_promote_proxmox_preserves_vm_fields(self) -> None:
        from testrange.backends.proxmox.orchestrator import (
            _promote_to_proxmox,
        )
        kwargs = _common_kwargs()
        # Use the generic Hypervisor — covers the
        # AbstractHypervisor branch which has the most fields.
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **kwargs)
        promoted = _promote_to_proxmox(hv)
        for field in ("name", "iso", "users", "pkgs",
                      "post_install_cmds", "devices", "builder",
                      "communicator", "orchestrator", "vms",
                      "networks"):
            assert getattr(promoted, field) == getattr(hv, field), (
                f"_promote_to_proxmox dropped field {field!r}"
            )

    def test_promote_libvirt_preserves_vm_fields(self) -> None:
        from testrange.backends.libvirt.orchestrator import (
            _promote_to_libvirt,
        )
        kwargs = _common_kwargs()
        hv = Hypervisor(orchestrator=LibvirtOrchestrator, **kwargs)
        promoted = _promote_to_libvirt(hv)
        for field in ("name", "iso", "users", "pkgs",
                      "post_install_cmds", "devices", "builder",
                      "communicator", "orchestrator", "vms",
                      "networks"):
            assert getattr(promoted, field) == getattr(hv, field), (
                f"_promote_to_libvirt dropped field {field!r}"
            )

    def test_promote_proxmox_network_preserves_fields(self) -> None:
        from testrange.backends.libvirt.network import (
            VirtualNetwork as LibvirtVirtualNetwork,
        )
        from testrange.backends.proxmox.network import (
            ProxmoxVirtualNetwork,
        )
        from testrange.backends.proxmox.orchestrator import (
            _promote_to_proxmox_network,
        )
        from testrange.networks import Switch
        sw = Switch("Corp", switch_type="vlan")
        src = LibvirtVirtualNetwork(
            "Net", "10.42.0.0/24",
            internet=True, dhcp=False, dns=False, switch=sw,
        )
        promoted = _promote_to_proxmox_network(src)
        assert isinstance(promoted, ProxmoxVirtualNetwork)
        for field in ("name", "subnet", "internet", "dhcp", "dns",
                      "switch"):
            assert getattr(promoted, field) == getattr(src, field), (
                f"_promote_to_proxmox_network dropped field {field!r}"
            )

    def test_promote_proxmox_switch_preserves_fields(self) -> None:
        from testrange.backends.proxmox.network import ProxmoxSwitch
        from testrange.backends.proxmox.orchestrator import (
            _promote_to_proxmox_switch,
        )
        from testrange.networks import Switch
        src = Switch("Corp", switch_type="vlan", uplinks=["eno1"])
        promoted = _promote_to_proxmox_switch(src)
        assert isinstance(promoted, ProxmoxSwitch)
        for field in ("name", "switch_type", "uplinks"):
            assert getattr(promoted, field) == getattr(src, field), (
                f"_promote_to_proxmox_switch dropped field {field!r}"
            )
