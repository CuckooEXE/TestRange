"""Tests for the top-level Plan + MockHypervisor + cross-reference checks."""

from __future__ import annotations

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.mock import MockHypervisor
from testrange.networks import Network, Sidecar, Switch
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
                NetworkIface(net),
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
        hyp = MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[_basic_recipe()],
        )
        plan = Plan("t", hyp)
        assert plan.hypervisor is hyp
        assert plan.name == "t"

    def test_no_hypervisor(self) -> None:
        with pytest.raises(ValueError, match="at least one hypervisor"):
            Plan("t")

    def test_name_missing_is_type_error(self) -> None:
        # name is the leading positional; omitting it entirely is a TypeError.
        with pytest.raises(TypeError):
            Plan()  # type: ignore[call-arg]

    def test_empty_name_rejected(self) -> None:
        hyp = MockHypervisor()
        with pytest.raises(ValueError, match="non-empty name"):
            Plan("", hyp)

    def test_multi_hypervisor_not_supported(self) -> None:
        hyp = MockHypervisor()
        with pytest.raises(NotImplementedError):
            Plan("t", hyp, hyp)


class TestMockHypervisor:
    def test_empty_ok(self) -> None:
        hyp = MockHypervisor()
        assert hyp.networks == ()
        assert hyp.vms == ()

    def test_vm_references_unknown_network(self) -> None:
        with pytest.raises(ValueError, match="unknown network"):
            MockHypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe(net="netZZ")],
            )

    def test_vm_references_unknown_pool(self) -> None:
        with pytest.raises(ValueError, match="unknown pool"):
            MockHypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe(pool="poolZZ")],
            )

    def test_duplicate_vm_names(self) -> None:
        with pytest.raises(ValueError, match="duplicate names"):
            MockHypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe("web"), _basic_recipe("web")],
            )

    def test_duplicate_network_names(self) -> None:
        with pytest.raises(ValueError, match="duplicate names"):
            MockHypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True)),
                    Switch("sw2", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True)),
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )

    def test_reserved_double_underscore_switch_rejected(self) -> None:
        # `__`-prefixed names belong to the orchestrator (__install, __uplink__*).
        with pytest.raises(ValueError, match="reserved"):
            MockHypervisor(
                networks=[Switch("__install", Network("netA"), cidr="10.0.0.0/24")],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )

    def test_reserved_double_underscore_network_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            MockHypervisor(
                networks=[Switch("sw1", Network("__uplink__sw1"), cidr="10.0.0.0/24")],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )

    def test_illegal_network_name_rejected(self) -> None:
        # Libvirt-specific charset rule is enforced here, not on Network().
        with pytest.raises(ValueError, match="illegal characters"):
            MockHypervisor(
                networks=[Switch("sw1", Network("net,a"), cidr="10.0.0.0/24")],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )

    def test_illegal_switch_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="illegal characters"):
            MockHypervisor(
                networks=[Switch("sw=1", Network("netA"), cidr="10.0.0.0/24")],
                pools=[StoragePool("pool1", 32)],
                vms=[],
            )

    def test_illegal_vm_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="illegal characters"):
            MockHypervisor(
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe("bad,name")],
            )

    def test_vm_name_data_disk_marker_rejected(self) -> None:
        # PVE-30: a VM named like another VM's data disk ('<vm>-data<i>') would
        # collide on the same volume ref. Reserve the marker.
        for bad in ("web-data0", "WEB-DATA1", "fs_data2", "node.data10"):
            with pytest.raises(ValueError, match="data<N>"):
                MockHypervisor(
                    networks=[
                        Switch(
                            "sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True)
                        )
                    ],
                    pools=[StoragePool("pool1", 32)],
                    vms=[_basic_recipe(bad)],
                )

    def test_vm_name_non_marker_data_allowed(self) -> None:
        # Only a *trailing* -data<N> marker is reserved; 'data' elsewhere is fine.
        MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[_basic_recipe("metadata-server"), _basic_recipe("data0-loader")],
        )


class TestVMRecipe:
    def test_credentials_lookup(self) -> None:
        r = _basic_recipe()
        # find_credential is CloudInitBuilder-specific (the orchestrator narrows
        # the same way — only CloudInitBuilder is supported in v0).
        builder = r.builder
        assert isinstance(builder, CloudInitBuilder)
        cred = builder.find_credential("u")
        assert cred is not None
        assert cred.username == "u"
        assert builder.find_credential("nope") is None
