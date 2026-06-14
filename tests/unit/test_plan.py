"""Tests for the ``Plan`` finalizer: validation + freeze + graph assembly.

``Plan(name, hyp)`` is where the 2.0 cut runs the whole-plan validation
(delegated to ``validate_hypervisor_plan``), seals the container, and
assembles the frozen :class:`BuildGraph` — including the implicit infra edges
inferred from typed handle references (ADR-0030, DAG-3/DAG-4).
"""

from __future__ import annotations

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.exceptions import (
    DanglingDependencyError,
    GraphCycleError,
    OrchestratorError,
)
from testrange.handles import NetworkHandle, PoolHandle
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockHypervisor


def _recipe(hyp: MockHypervisor, name: str = "web", *, extra: tuple[object, ...] = ()) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive(hyp.pools["pool1"], 8),
                NetworkIface(hyp.networks["netA"]),
                *extra,  # type: ignore[list-item]
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
            packages=[Apt("nginx")],
        ),
        communicator=SSHCommunicator("u"),
    )


def _populated() -> MockHypervisor:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True)))
    hyp.add_vm(_recipe(hyp))
    return hyp


class TestPlanBasics:
    def test_finalizes_and_exposes_graph(self) -> None:
        hyp = _populated()
        plan = Plan("t", hyp)
        assert plan.hypervisor is hyp
        assert plan.name == "t"
        assert plan.graph.names == ("pool:pool1", "network:sw1", "sidecar:sw1", "vm:web")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty name"):
            Plan("", _populated())

    def test_name_missing_is_type_error(self) -> None:
        with pytest.raises(TypeError):
            Plan()  # type: ignore[call-arg]

    def test_non_hypervisor_rejected(self) -> None:
        # The v0 `Plan(name, *hypervisors)` form factor is gone; a stray value
        # (or a v0-style varargs call) fails loud at the trust boundary.
        with pytest.raises(TypeError, match="takes a Hypervisor"):
            Plan("t", object())  # type: ignore[arg-type]


class TestImplicitInfraEdges:
    def test_vm_depends_on_its_pool_and_network(self) -> None:
        # The NIC's direct edge to network:sw1 and the OS-disk edge to pool:pool1
        # are kept; on a sidecar-carrying switch the VM also gates on the sidecar
        # barrier (DAG-23).
        plan = Plan("t", _populated())
        deps = {n.name for n in plan.graph.dependencies_of("vm:web")}
        assert deps == {"pool:pool1", "network:sw1", "sidecar:sw1"}

    def test_data_drive_pool_becomes_an_edge(self) -> None:
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_pool(StoragePool("pool2", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        )
        hyp.add_vm(_recipe(hyp, extra=(HardDrive(hyp.pools["pool2"], 16),)))
        plan = Plan("t", hyp)
        deps = {n.name for n in plan.graph.dependencies_of("vm:web")}
        assert "pool:pool2" in deps

    def test_sidecar_node_owns_the_pool_and_network_edges(self) -> None:
        # DAG-23: the sidecar's disk lands in the first declared pool and it
        # reads the switch's network backends, so the pool + network edges
        # attach to the SIDECAR node — the L2 fabric itself needs no storage.
        plan = Plan("t", _populated())
        assert plan.graph.dependencies_of("network:sw1") == ()
        deps = {n.name for n in plan.graph.dependencies_of("sidecar:sw1")}
        assert deps == {"pool:pool1", "network:sw1"}

    def test_sidecarless_switch_is_a_root(self) -> None:
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.0.0/24"))
        plan = Plan("t", hyp)
        assert plan.graph.dependencies_of("network:sw1") == ()

    def test_sidecar_without_pools_rejected(self) -> None:
        hyp = MockHypervisor()
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        )
        with pytest.raises(OrchestratorError, match=r"no.*pools"):
            Plan("t", hyp)


class TestExplicitEdges:
    def test_needs_orders_realize_waves(self) -> None:
        hyp = _populated()
        db = hyp.add_vm(_recipe(hyp, "db"))
        hyp.vms["web"].needs(db)
        plan = Plan("t", hyp)
        waves = [[n.name for n in wave] for wave in plan.graph.waves()]
        assert waves == [
            ["network:sw1", "pool:pool1"],
            ["sidecar:sw1"],
            ["vm:db"],
            ["vm:web"],
        ]
        # Builds stay concurrent: ordering edges don't gate materialize.
        assert [[n.name for n in w] for w in plan.graph.content_waves()] == [
            ["network:sw1", "pool:pool1", "sidecar:sw1", "vm:db", "vm:web"]
        ]

    def test_needs_cycle_rejected_at_plan(self) -> None:
        hyp = _populated()
        db = hyp.add_vm(_recipe(hyp, "db"))
        web = hyp.vms["web"]
        web.needs(db)
        db.needs(web)
        with pytest.raises(GraphCycleError, match="cycle"):
            Plan("t", hyp)

    def test_foreign_handle_is_a_dangling_reference(self) -> None:
        # A handle minted by hand (or borrowed from another container) that
        # matches no registered node surfaces as a graph validation error.
        hyp = _populated()
        hyp.vms["web"].needs(PoolHandle("not-registered"))
        with pytest.raises(DanglingDependencyError, match="not-registered"):
            Plan("t", hyp)


class TestPlanValidation:
    """The v0 whole-plan checks still run — now at Plan() time."""

    def test_unknown_network_handle_rejected(self) -> None:
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(
            Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
        )
        recipe = VMRecipe(
            spec=VMSpec(
                name="web",
                devices=[
                    CPU(1),
                    Memory(512),
                    OSDrive(hyp.pools["pool1"], 8),
                    # A hand-minted handle for a network nothing declared.
                    NetworkIface(NetworkHandle("netZZ", switch="swZZ")),
                ],
            ),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
            ),
            communicator=SSHCommunicator("u"),
        )
        hyp.add_vm(recipe)
        with pytest.raises(ValueError, match="unknown network"):
            Plan("t", hyp)

    def test_reserved_double_underscore_switch_rejected(self) -> None:
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(Switch("__install", Network("netA"), cidr="10.0.0.0/24"))
        with pytest.raises(ValueError, match="reserved"):
            Plan("t", hyp)

    def test_illegal_network_name_rejected(self) -> None:
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(Switch("sw1", Network("net,a"), cidr="10.0.0.0/24"))
        with pytest.raises(ValueError, match="illegal characters"):
            Plan("t", hyp)

    def test_illegal_vm_name_rejected(self) -> None:
        hyp = _populated()
        hyp.add_vm(_recipe(hyp, "bad,name"))
        with pytest.raises(ValueError, match="illegal characters"):
            Plan("t", hyp)

    def test_vm_name_data_disk_marker_rejected(self) -> None:
        # PVE-30: a VM named like another VM's data disk ('<vm>-data<i>') would
        # collide on the same volume ref. Reserve the marker.
        for bad in ("web-data0", "WEB-DATA1", "fs_data2", "node.data10"):
            hyp = _populated()
            hyp.add_vm(_recipe(hyp, bad))
            with pytest.raises(ValueError, match="data<N>"):
                Plan("t", hyp)

    def test_vm_name_non_marker_data_allowed(self) -> None:
        # Only a *trailing* -data<N> marker is reserved; 'data' elsewhere is fine.
        hyp = _populated()
        hyp.add_vm(_recipe(hyp, "metadata-server"))
        hyp.add_vm(_recipe(hyp, "data0-loader"))
        Plan("t", hyp)


class TestVMRecipe:
    def test_credentials_lookup(self) -> None:
        hyp = _populated()
        r = hyp.declared_vms[0]
        # find_credential is a builder-agnostic seam on the Builder ABC (CORE-66);
        # this recipe happens to use a CloudInitBuilder, but the lookup is the same
        # for any builder (see tests/unit/test_lookup_credential.py).
        builder = r.builder
        assert isinstance(builder, CloudInitBuilder)
        cred = builder.find_credential("u")
        assert cred is not None
        assert cred.username == "u"
        assert builder.find_credential("nope") is None
