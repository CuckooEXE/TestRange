"""Tests for the mutable ``Hypervisor`` node container (ADR-0030, DAG-4).

The 2.0 construction surface: ``add_pool``/``add_switch``/``add_vm`` register
a node and return its typed handle; registries expose handles by name with a
loud ``KeyError``; ``Plan(...)`` freezes the container. The generic type still
selects no driver (CORE-19) — the binding resolver rejects it without a
``--profile``.
"""

from __future__ import annotations

import pytest

from testrange import Hypervisor, Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import is_pinned, scheme_for_hypervisor
from testrange.handles import NetworkHandle, PoolHandle, SwitchHandle, VMHandle
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec


def _recipe(hyp: Hypervisor, name: str = "web") -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive(hyp.pools["pool1"], 8),
                NetworkIface(hyp.networks["netA"]),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
            packages=[Apt("nginx")],
        ),
        communicator=SSHCommunicator("u"),
    )


def _populated() -> Hypervisor:
    hyp = Hypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True)))
    hyp.add_vm(_recipe(hyp))
    return hyp


class TestRegistration:
    def test_add_pool_returns_typed_handle(self) -> None:
        hyp = Hypervisor()
        handle = hyp.add_pool(StoragePool("pool1", 32))
        assert isinstance(handle, PoolHandle)
        assert handle == "pool1"  # a handle IS the plan-level name
        assert handle.node_name == "pool:pool1"

    def test_add_switch_returns_handle_and_flattens_networks(self) -> None:
        hyp = Hypervisor()
        handle = hyp.add_switch(Switch("sw1", Network("netA"), Network("netB"), cidr="10.0.0.0/24"))
        assert isinstance(handle, SwitchHandle)
        assert set(hyp.networks) == {"netA", "netB"}
        net = hyp.networks["netA"]
        assert isinstance(net, NetworkHandle)
        assert net.switch == "sw1"
        # Both networks realize as the one switch unit, so both handles point
        # at the same graph node.
        assert net.node_name == hyp.networks["netB"].node_name == "network:sw1"

    def test_add_vm_returns_handle(self) -> None:
        hyp = _populated()
        assert isinstance(hyp.vms["web"], VMHandle)
        assert hyp.vms["web"].node_name == "vm:web"

    def test_registry_keyerror_is_loud_and_teaching(self) -> None:
        hyp = _populated()
        with pytest.raises(KeyError, match="no pool 'pool11'; known: pool1"):
            hyp.pools["pool11"]
        with pytest.raises(KeyError, match="no network 'netZZ'; known: netA"):
            hyp.networks["netZZ"]

    def test_declared_accessors_preserve_registration_order(self) -> None:
        hyp = Hypervisor()
        hyp.add_pool(StoragePool("b-pool", 1))
        hyp.add_pool(StoragePool("a-pool", 1))
        assert [p.name for p in hyp.declared_pools] == ["b-pool", "a-pool"]

    def test_duplicate_pool_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_pool(StoragePool("pool1", 64))

    def test_duplicate_vm_rejected_at_add(self) -> None:
        hyp = _populated()
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_vm(_recipe(hyp))

    def test_duplicate_network_across_switches_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24"))
        with pytest.raises(ValueError, match="already registered"):
            hyp.add_switch(Switch("sw2", Network("netA"), cidr="10.0.1.0/24"))

    def test_wrong_type_rejected_at_add(self) -> None:
        hyp = Hypervisor()
        with pytest.raises(TypeError, match="takes a StoragePool"):
            hyp.add_pool("pool1")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="takes a Switch"):
            hyp.add_switch(Network("netA"))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="takes a VMRecipe"):
            hyp.add_vm(StoragePool("pool1", 1))  # type: ignore[arg-type]


class TestFreeze:
    def test_plan_freezes_container(self) -> None:
        hyp = _populated()
        assert hyp.frozen is False
        Plan("t", hyp)
        assert hyp.frozen is True
        with pytest.raises(ValueError, match="frozen"):
            hyp.add_pool(StoragePool("late", 1))
        with pytest.raises(ValueError, match="frozen"):
            hyp.add_explicit_edge("vm:web", "pool:pool1")

    def test_needs_after_freeze_rejected(self) -> None:
        hyp = _populated()
        web = hyp.vms["web"]
        other = hyp.add_vm(_recipe(hyp, "db"))
        Plan("t", hyp)
        with pytest.raises(ValueError, match="frozen"):
            web.needs(other)


class TestExplicitEdges:
    def test_needs_records_edges_in_order(self) -> None:
        hyp = _populated()
        db = hyp.add_vm(_recipe(hyp, "db"))
        web = hyp.vms["web"]
        web.needs(db)
        assert hyp.explicit_edges == (("vm:web", "vm:db"),)

    def test_needs_self_rejected(self) -> None:
        hyp = _populated()
        with pytest.raises(ValueError, match="cannot need itself"):
            hyp.vms["web"].needs(hyp.vms["web"])

    def test_needs_non_handle_rejected(self) -> None:
        hyp = _populated()
        with pytest.raises(TypeError, match="takes handles"):
            hyp.vms["web"].needs("db")  # type: ignore[arg-type]


class TestBuildSwitch:
    def test_build_switch_is_portable_topology(self) -> None:
        # ADR-0016: uplink is a profile-resolved logical name, so the build
        # switch carries nothing host-specific and lives on the Hypervisor.
        assert Hypervisor().build_switch is None
        bs = Switch("build", Network("b"), cidr="10.97.99.0/24", sidecar=Sidecar(dhcp=True))
        assert Hypervisor(build_switch=bs).build_switch is bs


class TestSchemePin:
    def test_generic_not_pinned(self) -> None:
        # Unregistered by design: it selects no scheme. is_pinned must report
        # False so the binding resolver routes it through the --profile path.
        hyp = _populated()
        assert is_pinned(hyp) is False
        assert scheme_for_hypervisor(hyp) is None
