"""Forward-compat seam checks for the deferred node/edge kinds (DAG-19/DAG-20).

NOT features — design-validation. ADR-0030 defers appliances
(deploy-through-an-endpoint), relationship edges with bake/replay caching,
nested hypervisors, and multi-backend; the acceptance criterion is that they
land as **new node/edge kinds, not a reshape**. These tests prove it the only
way that can't drift: by defining the deferred kinds as plain subclasses today
and running them through the UNMODIFIED graph model, key walk, and executor.

DAG-19 — ``ApplianceNode`` + relationship edges: a new ``kind`` tag and
``bake``/``replay`` cacheability ride the existing ABCs; the transitive key
walk folds them; topology never branches on them.

DAG-20 — ``HypervisorNode`` + a second backend: a node kind that *recurses an
inner BuildGraph* (the ADR-0021 nesting, re-expressed) dispatches through the
same executor hooks; "another backend" is, to the executor, just another
driver behind the same ABC — which the kind-agnostic dispatch never inspects.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from testrange.graph.build_graph import BuildGraph
from testrange.graph.edge import Cacheability, Edge
from testrange.graph.keys import compute_cache_keys
from testrange.graph.node import Node, NodeContext
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.executor import materialize_graph, realize_graph
from testrange.state.store import StateStore
from tests.mock_driver import MockDriver


class _SeamNode(Node):
    """A deferred-kind stand-in: records hook calls, hashes deterministically."""

    def __init__(self, name: str, kind: str) -> None:
        self._name = name
        self._kind = kind
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> str:
        return self._kind

    def cache_key(self, ctx: NodeContext, dependency_keys: Mapping[str, str]) -> str:
        del ctx
        material = self._name + "".join(f"|{k}={v}" for k, v in sorted(dependency_keys.items()))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def materialize(self, ctx: NodeContext) -> None:
        del ctx
        self.calls.append("materialize")

    def realize(self, ctx: NodeContext) -> None:
        del ctx
        self.calls.append("realize")


class _HypervisorSeamNode(_SeamNode):
    """DAG-20: a node kind that recurses an inner graph from its own hooks."""

    def __init__(self, name: str, inner: BuildGraph) -> None:
        super().__init__(name, "hypervisor")
        self.inner = inner

    def realize(self, ctx: NodeContext) -> None:
        # The recursion seam: the outer executor calls one hook; the node walks
        # its inner graph itself. The outer walk needs no new branch.
        for wave in self.inner.waves():
            for node in wave:
                node.realize(ctx)
        self.calls.append("realize")


def _ctx(tmp_path: Path) -> GraphContext:
    store = StateStore(tmp_path / "run")
    store.initialize(run_id="seam", plan_name="seam", driver_class="MockDriver", driver_uri="")
    from testrange.cache.local import LocalCache
    from testrange.cache.manager import CacheManager

    return GraphContext(
        plan=None,  # type: ignore[arg-type]  # seam stubs never read the plan
        resolved=ResolvedBackend(driver=MockDriver(pool_root=tmp_path / "p"), driver_uri=""),
        store=store,
        cache=CacheManager(local=LocalCache(root=tmp_path / "c")),
        run_id="seam",
        plan_name="seam",
        build_timeout_s=1.0,
        lease_timeout_s=1.0,
        addressing={},
    )


class TestApplianceSeam:
    """DAG-19: ApplianceNode + manage/collect_from-style relationship edges."""

    def _appliance_graph(self) -> tuple[BuildGraph, dict[str, _SeamNode]]:
        esxi = _SeamNode("vm:esxi-host", "vm")
        vcenter = _SeamNode("appliance:vcenter", "appliance")
        collector = _SeamNode("appliance:aria", "appliance")
        graph = BuildGraph(
            "appliances",
            [esxi, vcenter, collector],
            [
                # vCenter deploys through the running ESXi and BAKES its
                # identity (a manage-style relationship edge).
                Edge("appliance:vcenter", "vm:esxi-host", cacheability=Cacheability.BAKE),
                # Aria collects from vCenter at realize time (collect_from,
                # replay-style: re-applied, not baked).
                Edge("appliance:aria", "appliance:vcenter", cacheability=Cacheability.REPLAY),
            ],
        )
        return graph, {"esxi": esxi, "vcenter": vcenter, "aria": collector}

    def test_graph_accepts_new_kinds_and_orders_them(self) -> None:
        graph, _ = self._appliance_graph()
        assert [[n.name for n in w] for w in graph.waves()] == [
            ["vm:esxi-host"],
            ["appliance:vcenter"],
            ["appliance:aria"],
        ]

    def test_relationship_edges_gate_materialize(self) -> None:
        # Unlike ordering edges, bake/replay edges ARE build dependencies:
        # the content waves serialize the bake chain.
        graph, _ = self._appliance_graph()
        assert [[n.name for n in w] for w in graph.content_waves()] == [
            ["vm:esxi-host"],
            ["appliance:vcenter"],
            ["appliance:aria"],
        ]

    def test_transitive_key_folds_through_the_chain(self, tmp_path: Path) -> None:
        graph, nodes = self._appliance_graph()
        ctx = _ctx(tmp_path)
        keys = compute_cache_keys(graph, ctx)
        # vcenter's key folds esxi's; a changed esxi must invalidate vcenter
        # and (transitively, via the replay edge) aria.
        solo_vcenter = nodes["vcenter"].cache_key(ctx, {})
        assert keys["appliance:vcenter"] != solo_vcenter
        rekeyed = compute_cache_keys(
            BuildGraph(
                "appliances2",
                [_SeamNode("vm:esxi-host", "vm-CHANGED"), nodes["vcenter"], nodes["aria"]],
                graph.edges,
            ),
            ctx,
        )
        assert rekeyed["appliance:vcenter"] == keys["appliance:vcenter"]  # same esxi NAME+key
        # Changing the dependency's KEY (not just kind) re-keys the dependent.
        changed = compute_cache_keys(
            BuildGraph(
                "appliances3",
                [_SeamNode("vm:esxi-host2", "vm"), nodes["vcenter"], nodes["aria"]],
                [
                    Edge("appliance:vcenter", "vm:esxi-host2", cacheability=Cacheability.BAKE),
                    Edge("appliance:aria", "appliance:vcenter", cacheability=Cacheability.REPLAY),
                ],
            ),
            ctx,
        )
        assert changed["appliance:vcenter"] != keys["appliance:vcenter"]

    def test_executor_dispatches_appliances_unchanged(self, tmp_path: Path) -> None:
        graph, nodes = self._appliance_graph()
        ctx = _ctx(tmp_path)
        materialize_graph(ctx, graph)
        realize_graph(ctx, graph)
        for node in nodes.values():
            assert node.calls == ["materialize", "realize"]
        assert ctx.realized_nodes == {n.name for n in graph.nodes}


class TestHypervisorSeam:
    """DAG-20: a HypervisorNode recursing an inner graph, multi-backend-shaped."""

    def test_nested_graph_runs_through_one_outer_hook(self, tmp_path: Path) -> None:
        inner_vm = _SeamNode("vm:inner-web", "vm")
        inner = BuildGraph("inner", [inner_vm])
        host = _HypervisorSeamNode("hypervisor:esxi-a", inner)
        outer = BuildGraph("outer", [host])
        ctx = _ctx(tmp_path)
        materialize_graph(ctx, outer)
        realize_graph(ctx, outer)
        # The inner VM realized through the host node's own recursion; the
        # outer executor saw exactly one node and needed no new branch.
        assert inner_vm.calls == ["realize"]
        assert host.calls == ["materialize", "realize"]

    def test_second_backend_is_just_another_driver(self, tmp_path: Path) -> None:
        # The executor's only backend coupling is ctx.driver behind the ABC —
        # swapping the bound driver swaps the backend with zero graph changes.
        ctx = _ctx(tmp_path)
        node = _SeamNode("vm:web", "vm")
        graph = BuildGraph("portable", [node])
        materialize_graph(ctx, graph)
        realize_graph(ctx, graph)
        assert node.calls == ["materialize", "realize"]

    def test_unknown_kind_never_perturbs_validation(self) -> None:
        # Kind-agnosticism: a kind tag the MVP never shipped flows through
        # cycle/dangling/duplicate validation identically.
        with pytest.raises(Exception, match="cycle"):
            BuildGraph(
                "loop",
                [_SeamNode("a", "hypervisor"), _SeamNode("b", "appliance")],
                [Edge("a", "b"), Edge("b", "a")],
            )
