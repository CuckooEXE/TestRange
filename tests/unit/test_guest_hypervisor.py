"""Tests for GuestHypervisor — a VMRecipe that is also a host (CORE-38)."""

from __future__ import annotations

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.utils import SSHKey
from testrange.vms import GuestHypervisor, VMRecipe, VMSpec
from tests.mock_driver import MockHypervisor

_KEY = SSHKey.generate(comment="nested-test")
_ADMIN = PosixCred("admin", ssh_key=_KEY, admin=True)


def _outer_spec(name: str = "host-a", pool: str = "pool1", net: str = "lab-net") -> VMSpec:
    return VMSpec(
        name=name,
        devices=[
            CPU(4, nested=True),
            Memory(8192),
            OSDrive(pool, 40),
            NetworkIface(net, addr=DHCPAddr()),
        ],
    )


def _inner_vm() -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name="webapp",
            devices=[CPU(1), Memory(1024), OSDrive("inner-pool", 8), NetworkIface("inner-net")],
        ),
        builder=CloudInitBuilder(base=CacheEntry("debian-13")),
        communicator=NativeCommunicator(),
    )


def _inner_libvirt() -> LibvirtHypervisor:
    return LibvirtHypervisor(
        networks=[Switch("inner", Network("inner-net"), cidr="192.168.50.0/24")],
        pools=[StoragePool("inner-pool", 32)],
        vms=[_inner_vm()],
    )


def _guest(name: str = "host-a") -> GuestHypervisor:
    return GuestHypervisor(
        spec=_outer_spec(name),
        builder=CloudInitBuilder(base=CacheEntry("debian-13"), credentials=[_ADMIN]),
        communicator=SSHCommunicator("admin"),
        inner=_inner_libvirt(),
    )


class TestGuestHypervisorShape:
    def test_is_a_vmrecipe(self) -> None:
        # The whole point: build/run/bind treat it as an ordinary VM; only
        # nested_phase narrows with isinstance(vm, GuestHypervisor).
        g = _guest()
        assert isinstance(g, VMRecipe)
        assert isinstance(g, GuestHypervisor)

    def test_exposes_recipe_surface_and_inner(self) -> None:
        g = _guest()
        assert g.name == "host-a"
        assert g.spec.cpu.nested is True
        assert isinstance(g.inner, LibvirtHypervisor)
        assert [v.name for v in g.inner.vms] == ["webapp"]

    def test_frozen(self) -> None:
        g = _guest()
        with pytest.raises(AttributeError):
            g.inner = _inner_libvirt()  # type: ignore[misc]


class TestLibvirtSugar:
    def test_fills_stack_builder_and_ssh(self) -> None:
        g = GuestHypervisor.libvirt(
            spec=_outer_spec(),
            admin=_ADMIN,
            networks=[Switch("inner", Network("inner-net"), cidr="192.168.50.0/24")],
            pools=[StoragePool("inner-pool", 32)],
            vms=[_inner_vm()],
        )
        assert isinstance(g.inner, LibvirtHypervisor)
        assert isinstance(g.communicator, SSHCommunicator)
        assert g.communicator.username == "admin"
        assert isinstance(g.builder, CloudInitBuilder)
        apt_names = {p.name for p in g.builder.packages if isinstance(p, Apt)}
        assert {"qemu-system-x86", "libvirt-daemon-system", "libvirt-clients"} <= apt_names
        # admin credential carried onto the builder so the inner qemu+ssh binding
        # and outer SSH login share the baked key.
        assert g.builder.find_credential("admin") is not None
        # libvirtd brought up and the admin joined to the libvirt group.
        joined = "\n".join(g.builder.post_install_commands)
        assert "libvirtd" in joined
        assert "admin" in joined and "libvirt" in joined

    def test_extra_packages_appended(self) -> None:
        g = GuestHypervisor.libvirt(spec=_outer_spec(), admin=_ADMIN, extra_packages=[Apt("ovmf")])
        assert isinstance(g.builder, CloudInitBuilder)
        apt_names = {p.name for p in g.builder.packages if isinstance(p, Apt)}
        assert "ovmf" in apt_names
        assert "qemu-system-x86" in apt_names  # stack still present


class TestPlanValidation:
    def _outer(self, vms: list[VMRecipe]) -> MockHypervisor:
        return MockHypervisor(
            networks=[
                Switch("lab", Network("lab-net"), cidr="10.50.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 128)],
            vms=vms,
        )

    def test_guest_in_vms_validates(self) -> None:
        hyp = self._outer([_guest()])
        plan = Plan("nested-demo", hyp)
        assert plan.hypervisor.vms[0].name == "host-a"

    def test_guest_name_collision_with_plain_vm_rejected(self) -> None:
        plain = VMRecipe(
            spec=VMSpec(name="host-a", devices=[CPU(1), Memory(512), OSDrive("pool1", 8)]),
            builder=CloudInitBuilder(base=CacheEntry("debian-13")),
            communicator=NativeCommunicator(),
        )
        with pytest.raises(ValueError, match="duplicate names"):
            self._outer([_guest("host-a"), plain])

    def test_guest_osdrive_unknown_outer_pool_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown pool"):
            self._outer([_guest_with_pool("nope")])


def _guest_with_pool(pool: str) -> GuestHypervisor:
    return GuestHypervisor(
        spec=_outer_spec(pool=pool),
        builder=CloudInitBuilder(base=CacheEntry("debian-13"), credentials=[_ADMIN]),
        communicator=SSHCommunicator("admin"),
        inner=_inner_libvirt(),
    )
