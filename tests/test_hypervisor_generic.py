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

    def test_proxmox_inner_injects_dnsmasq(self) -> None:
        # PVE installer is the whole install phase, but TestRange's
        # SDN dnsmasq integration needs the ``dnsmasq`` apt package
        # on the node — ProxmoxOrchestrator.prepare_outer_vm stamps
        # it onto the spec so the nested case satisfies its own
        # _preflight_dnsmasq_installed by construction.  Nothing else
        # gets injected (the PVE installer ISO covers pveproxy + the
        # rest), and post_install_cmds stays empty.
        hv = Hypervisor(orchestrator=ProxmoxOrchestrator, **_common_kwargs())
        assert [p.name for p in hv.pkgs] == ["dnsmasq"]
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
        # ProxmoxOrchestrator.prepare_outer_vm injects ``dnsmasq``
        # (needed for the SDN-dnsmasq integration the inner orch
        # relies on).  No libvirt-daemon-system packages — those
        # belong to the outer-libvirt-inner-libvirt path only.
        assert [p.name for p in promoted.pkgs] == ["dnsmasq"]

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
        # ``dnsmasq`` injected by prepare_outer_vm survives the
        # cross-backend hop (same shape as the libvirt-inner test
        # above, just with proxmox-on-proxmox instead).
        assert [p.name for p in promoted.pkgs] == ["dnsmasq"]


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
