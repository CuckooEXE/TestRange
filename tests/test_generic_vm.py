"""Tests for :class:`testrange.GenericVM` and the orchestrator's
GenericVM → LibvirtVM promotion path.

GenericVM is the backend-agnostic counterpart of every backend's
concrete VM class — same constructor surface, no implementation.
The orchestrator's ``__init__`` translates each GenericVM to its
own native VM type so the rest of the provisioning code never
sees a GenericVM.
"""

from __future__ import annotations

import pytest

from testrange import (
    Credential,
    GenericVM,
    LibvirtVM,
    Memory,
    Orchestrator,
    vCPU,
    vNIC,
)
from testrange.exceptions import VMBuildError


def _spec(name: str = "web") -> GenericVM:
    return GenericVM(
        name=name,
        iso="https://example.com/x.qcow2",
        users=[Credential("root", "pw")],
        devices=[vCPU(1), Memory(1), vNIC("Net")],
    )


class TestGenericVMConstruction:
    """The same constructor surface every backend's VM has."""

    def test_basic_spec(self) -> None:
        vm = _spec("web01")
        assert vm.name == "web01"
        assert vm.iso == "https://example.com/x.qcow2"
        assert len(vm.devices) == 3

    def test_auto_selects_builder(self) -> None:
        """GenericVM gets the same auto-builder selection backend VMs do."""
        vm = _spec()
        from testrange.vms.builders.cloud_init import CloudInitBuilder
        assert isinstance(vm.builder, CloudInitBuilder)

    def test_communicator_default_from_builder(self) -> None:
        vm = _spec()
        # CloudInitBuilder.default_communicator() == "guest-agent"
        assert vm.communicator == "guest-agent"

    def test_unknown_communicator_rejected(self) -> None:
        with pytest.raises(VMBuildError, match="communicator="):
            GenericVM(
                "x", "y", [Credential("r", "p")], communicator="bogus",
            )


class TestGenericVMIsSibling:
    """GenericVM is a sibling of LibvirtVM under AbstractVM, NOT a
    parent/child of it.  Same architecture as the device split — that's
    what keeps the type system honest about backend compatibility."""

    def test_is_an_abstract_vm(self) -> None:
        from testrange.vms.base import AbstractVM
        assert isinstance(_spec(), AbstractVM)

    def test_is_not_a_libvirt_vm(self) -> None:
        """GenericVM is NOT-A LibvirtVM — promoting requires explicit
        construction, not isinstance.  Otherwise type narrowing in
        backends would silently accept a GenericVM as if it were
        backend-specific."""
        assert not isinstance(_spec(), LibvirtVM)

    def test_libvirt_vm_is_not_a_generic_vm(self) -> None:
        """And the reverse: LibvirtVM is NOT-A GenericVM."""
        lvm = LibvirtVM(
            "x", "y", [Credential("r", "p")], devices=[vCPU(1), Memory(1)],
        )
        assert not isinstance(lvm, GenericVM)


class TestGenericVMRefusesBackendOps:
    """A GenericVM that escapes to the provisioning code paths is a
    wiring bug — calling backend ops should surface it loudly."""

    def test_build_raises(self) -> None:
        with pytest.raises(VMBuildError, match="backend operation"):
            _spec().build(None, None, None, "n", "00:00:00:00:00:00")  # type: ignore[arg-type]

    def test_start_run_raises(self) -> None:
        with pytest.raises(VMBuildError, match="backend operation"):
            _spec().start_run(None, None, "d", [], [])  # type: ignore[arg-type]

    def test_shutdown_raises(self) -> None:
        with pytest.raises(VMBuildError, match="backend operation"):
            _spec().shutdown()


class TestOrchestratorPromotion:
    """Orchestrator's __init__ converts every GenericVM in vms= to
    its own backend-native VM type.  After construction the user
    should only ever see backend-native VMs in ``_vm_list``."""

    def test_generic_vm_promoted_to_libvirt_vm(self) -> None:
        orch = Orchestrator(vms=[_spec("web")])
        assert all(isinstance(v, LibvirtVM) for v in orch._vm_list)
        assert not any(isinstance(v, GenericVM) for v in orch._vm_list)

    def test_libvirt_vm_passes_through_unchanged(self) -> None:
        lvm = LibvirtVM(
            name="web",
            iso="https://example.com/x.qcow2",
            users=[Credential("root", "pw")],
            devices=[vCPU(1), Memory(1)],
        )
        orch = Orchestrator(vms=[lvm])
        # Same object — promotion is a no-op when input is already native.
        assert orch._vm_list[0] is lvm

    def test_mixed_specs_all_promote_to_libvirt(self) -> None:
        generic = _spec("a")
        native = LibvirtVM(
            name="b",
            iso="https://example.com/y.qcow2",
            users=[Credential("root", "pw")],
            devices=[vCPU(1), Memory(1)],
        )
        orch = Orchestrator(vms=[generic, native])
        assert [v.name for v in orch._vm_list] == ["a", "b"]
        assert all(isinstance(v, LibvirtVM) for v in orch._vm_list)

    def test_preserves_spec_fields(self) -> None:
        """Promotion is field-for-field — no spec data is lost."""
        spec = GenericVM(
            name="db",
            iso="https://example.com/z.qcow2",
            users=[Credential("admin", "pw", sudo=True)],
            pkgs=[],
            post_install_cmds=["echo hi"],
            devices=[vCPU(4), Memory(8), vNIC("Net", ip="10.0.0.5")],
        )
        orch = Orchestrator(vms=[spec])
        promoted = orch._vm_list[0]

        assert promoted.name == spec.name
        assert promoted.iso == spec.iso
        assert promoted.users == spec.users
        assert promoted.post_install_cmds == spec.post_install_cmds
        assert len(promoted.devices) == len(spec.devices)
        assert promoted.communicator == spec.communicator
