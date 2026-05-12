"""Tests for the top-level Plan + LibvirtHypervisor + cross-reference checks."""

from __future__ import annotations

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec


def _basic_recipe(name: str = "web", net: str = "netA", pool: str = "pool1") -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive(pool, 8),
                LibvirtNetworkIface(net),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
            packages=[Apt("nginx")],
        ),
        communicator=SSHCommunicator("u"),
    )


class TestPlan:
    def test_single_hypervisor(self) -> None:
        hyp = LibvirtHypervisor(
            connection="qemu:///session",
            networks=[Switch("sw1", Network("netA", "10.0.0.0/24"))],
            pools=[StoragePool("pool1", 32)],
            vms=[_basic_recipe()],
        )
        plan = Plan(hyp)
        assert plan.hypervisor is hyp

    def test_empty_plan(self) -> None:
        with pytest.raises(ValueError):
            Plan()

    def test_multi_hypervisor_not_supported(self) -> None:
        hyp = LibvirtHypervisor(connection="qemu:///session")
        with pytest.raises(NotImplementedError):
            Plan(hyp, hyp)


class TestLibvirtHypervisor:
    def test_empty_ok(self) -> None:
        hyp = LibvirtHypervisor(connection="qemu:///session")
        assert hyp.networks == ()
        assert hyp.vms == ()

    def test_vm_references_unknown_network(self) -> None:
        with pytest.raises(ValueError, match="unknown network"):
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[Switch("sw1", Network("netA", "10.0.0.0/24"))],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe(net="netZZ")],
            )

    def test_vm_references_unknown_pool(self) -> None:
        with pytest.raises(ValueError, match="unknown pool"):
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[Switch("sw1", Network("netA", "10.0.0.0/24"))],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe(pool="poolZZ")],
            )

    def test_duplicate_vm_names(self) -> None:
        with pytest.raises(ValueError, match="duplicate names"):
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[Switch("sw1", Network("netA", "10.0.0.0/24"))],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe("web"), _basic_recipe("web")],
            )

    def test_duplicate_network_names(self) -> None:
        with pytest.raises(ValueError, match="duplicate names"):
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[
                    Switch("sw1", Network("netA", "10.0.0.0/24")),
                    Switch("sw2", Network("netA", "10.0.1.0/24")),
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )


class TestVMRecipe:
    def test_credentials_lookup(self) -> None:
        r = _basic_recipe()
        cred = r.builder.find_credential("u")
        assert cred is not None
        assert cred.username == "u"
        assert r.builder.find_credential("nope") is None
