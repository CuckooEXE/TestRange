"""Tests for the MVP node kinds and the per-node cache key (DAG-3/DAG-5).

The load-bearing check here is **v0 key parity**: an MVP graph carries only
ordering edges, so a VM node's key must be byte-identical to the v0 per-build
``builder.config_hash`` for the same plan/profile/cache — no spurious cache
busting across the 2.0 cut (ADR-0030).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.graph.build_graph import BuildGraph
from testrange.graph.edge import Cacheability, Edge
from testrange.graph.keys import compute_cache_keys
from testrange.networks import Network, Sidecar, Switch
from testrange.nodes import NetworkNode, PoolNode, VMNode
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.vm_build import build_nic_for, resolve_sidecar_sha
from testrange.state.store import StateStore
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor


def _plan() -> Plan:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True)))
    hyp.add_vm(
        VMRecipe(
            spec=VMSpec(
                name="web",
                devices=[
                    CPU(1),
                    Memory(512),
                    OSDrive(hyp.pools["pool1"], 8),
                    NetworkIface(hyp.networks["netA"], addr=StaticAddr("172.31.0.150")),
                ],
            ),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"),
                credentials=[PosixCred("u", password="p")],
            ),
            communicator=SSHCommunicator("u"),
        )
    )
    return Plan("parity", hyp)


@pytest.fixture
def ctx(tmp_path: Path) -> GraphContext:
    cache = LocalCache(root=tmp_path / "c")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"BASE" * 64)
    cache.add(base, name="debian-13")
    sidecar = tmp_path / "sidecar.qcow2"
    sidecar.write_bytes(b"SIDECAR" * 64)
    cache.add(sidecar, name="testrange-sidecar")
    plan = _plan()
    from testrange.networks.base import NetworkAddressing

    return GraphContext(
        plan=plan,
        resolved=ResolvedBackend(driver=MockDriver(pool_root=tmp_path / "p"), driver_uri=""),
        store=StateStore(tmp_path / "run"),
        cache=CacheManager(local=cache),
        run_id="r1",
        plan_name=plan.name,
        build_timeout_s=1.0,
        lease_timeout_s=1.0,
        addressing={
            n.name: NetworkAddressing.from_switch(s)
            for s in plan.hypervisor.declared_switches
            for n in s.networks
        },
    )


class TestNodeIdentity:
    def test_kinds_and_names(self) -> None:
        plan = _plan()
        g = plan.graph
        # sw1 carries a sidecar, so it gets its own SidecarNode (DAG-23). The
        # L2 fabric (network:sw1) has no deps now, so it shares wave 0 with the
        # pool; the sidecar gates on both, and the VM gates on the sidecar.
        assert [(n.name, n.kind) for n in g.topological_order()] == [
            ("network:sw1", "network"),
            ("pool:pool1", "pool"),
            ("sidecar:sw1", "sidecar"),
            ("vm:web", "vm"),
        ]

    def test_sidecar_owns_the_pool_and_network_edges(self) -> None:
        """DAG-23: the storage-pool dependency lands on the sidecar, not the L2
        switch; the VM gates on the sidecar (the barrier), which gates on the
        fabric + pool. The network node depends on nothing."""
        g = _plan().graph

        def deps(name: str) -> set[str]:
            return {d.name for d in g.dependencies_of(name)}

        assert deps("network:sw1") == set()  # L2 fabric needs no storage
        assert deps("sidecar:sw1") == {"network:sw1", "pool:pool1"}
        # VM keeps its direct NIC->network and OS-disk->pool edges, plus the
        # sidecar barrier edge.
        assert deps("vm:web") == {"network:sw1", "sidecar:sw1", "pool:pool1"}

    def test_sidecarless_switch_has_no_sidecar_node(self) -> None:
        """An air-gapped switch (no sidecar) gets no SidecarNode; the VM's NIC
        edge targets the network node directly, and no pool edge is invented."""
        hyp = MockHypervisor()
        hyp.add_pool(StoragePool("pool1", 32))
        hyp.add_switch(Switch("air", Network("netA"), cidr="10.9.0.0/24"))  # no sidecar
        hyp.add_vm(
            VMRecipe(
                spec=VMSpec(
                    name="iso",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive(hyp.pools["pool1"], 8),
                        NetworkIface(hyp.networks["netA"], addr=StaticAddr("10.9.0.5")),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
                ),
                communicator=SSHCommunicator("u"),
            )
        )
        g = Plan("airgap", hyp).graph
        assert "sidecar:air" not in g
        assert {n.kind for n in g.nodes} == {"pool", "network", "vm"}
        assert {d.name for d in g.dependencies_of("network:air")} == set()
        assert {d.name for d in g.dependencies_of("vm:iso")} == {"network:air", "pool:pool1"}

    def test_infra_keys_are_pure_declaration(self, ctx: GraphContext) -> None:
        pool = PoolNode(StoragePool("pool1", 32))
        assert pool.cache_key(ctx, {}) == pool.cache_key(ctx, {})
        assert pool.cache_key(ctx, {}) != PoolNode(StoragePool("pool1", 64)).cache_key(ctx, {})
        sw = Switch("sw1", Network("netA"), cidr="10.0.0.0/24")
        assert NetworkNode(sw).cache_key(ctx, {}) == NetworkNode(sw).cache_key(ctx, {})


class TestV0KeyParity:
    def test_vm_key_equals_v0_config_hash(self, ctx: GraphContext) -> None:
        """DAG-5 regression: the graph walk's VM key IS the v0 config_hash.

        The right-hand side replicates the v0 probe exactly: resolve the
        origin + sidecar shas, compose the stable MACs, synthesize the build
        NIC for plan position 0, and call ``builder.config_hash`` — the same
        call the v0 ``_probe_vm`` made. An MVP graph (ordering edges only)
        must produce the identical key, or every existing cache entry would
        bust on the 2.0 upgrade.
        """
        plan = ctx.plan
        keys = compute_cache_keys(plan.graph, ctx)

        vm = plan.hypervisor.declared_vms[0]
        builder = vm.builder
        base = builder.os_disk_base()
        assert base is not None
        base_sha = ctx.cache.resolve(base, fetch=False).sha256
        sidecar_sha = resolve_sidecar_sha(ctx)
        macs = tuple(
            ctx.driver.compose_mac(ctx.plan_name, vm.name, i) for i in range(len(vm.spec.nics))
        )
        build_nic = build_nic_for(ctx, resolve_build_switch(plan.hypervisor.build_switch), vm, 0)
        v0_key = builder.config_hash(
            vm.spec,
            vm,
            addressing=ctx.addressing,
            base_sha=base_sha,
            sidecar_sha=sidecar_sha,
            macs=macs,
            build_nic=build_nic,
            native_agent=None,
        )
        assert keys["vm:web"] == v0_key

    def test_probe_is_ledgered_for_materialize(self, ctx: GraphContext) -> None:
        compute_cache_keys(ctx.plan.graph, ctx)
        probe = ctx.vm_probes["vm:web"]
        assert probe.config_hash == ctx.node_keys.get("vm:web", probe.config_hash)
        assert probe.cached_paths is None  # nothing built yet -> whole-VM miss


class TestTransitiveFold:
    def test_ordering_edge_does_not_invalidate(self, ctx: GraphContext) -> None:
        """Placement is not invalidation: ordering edges leave keys unchanged."""
        plan = ctx.plan
        base_keys = compute_cache_keys(plan.graph, ctx)
        nodes = list(plan.graph.nodes)
        extra = BuildGraph(
            "with-ordering",
            nodes,
            [*plan.graph.edges, Edge("vm:web", "pool:pool1", cacheability=Cacheability.ORDERING)],
        )
        assert compute_cache_keys(extra, ctx)["vm:web"] == base_keys["vm:web"]

    @pytest.mark.parametrize("cacheability", [Cacheability.BAKE, Cacheability.REPLAY])
    def test_cacheable_edge_folds_upstream_key(
        self, ctx: GraphContext, cacheability: Cacheability
    ) -> None:
        """A bake/replay edge folds the dependency's key into the dependent's."""
        plan = ctx.plan
        base_keys = compute_cache_keys(plan.graph, ctx)
        nodes = list(plan.graph.nodes)
        baked = BuildGraph(
            "with-bake",
            nodes,
            [*plan.graph.edges, Edge("vm:web", "pool:pool1", cacheability=cacheability)],
        )
        keys = compute_cache_keys(baked, ctx)
        assert keys["vm:web"] != base_keys["vm:web"]
        assert keys["pool:pool1"] == base_keys["pool:pool1"]

    def test_strongest_wins_across_parallel_edges(self, ctx: GraphContext) -> None:
        """An ordering edge alongside a bake edge never weakens invalidation."""
        plan = ctx.plan
        nodes = list(plan.graph.nodes)
        both = BuildGraph(
            "parallel-edges",
            nodes,
            [
                *plan.graph.edges,
                Edge("vm:web", "pool:pool1", cacheability=Cacheability.ORDERING),
                Edge("vm:web", "pool:pool1", cacheability=Cacheability.BAKE),
            ],
        )
        bake_only = BuildGraph(
            "bake-edge",
            nodes,
            [*plan.graph.edges, Edge("vm:web", "pool:pool1", cacheability=Cacheability.BAKE)],
        )
        assert (
            compute_cache_keys(both, ctx)["vm:web"] == compute_cache_keys(bake_only, ctx)["vm:web"]
        )


class TestVMNodeGuards:
    def test_materialize_before_key_walk_fails_loud(self, ctx: GraphContext) -> None:
        from testrange.exceptions import OrchestratorError

        node = next(n for n in ctx.plan.graph.nodes if isinstance(n, VMNode))
        with pytest.raises(OrchestratorError, match="key walk"):
            node.materialize(ctx)
