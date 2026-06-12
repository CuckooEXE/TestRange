"""Unit tests for the pure build-graph model (DAG-2, ADR-0030).

Covers the :class:`Node` / :class:`Edge` contracts and the
:class:`BuildGraph` validation + topological-wave algorithm. No backend, no
driver — this is the pure-model layer.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Mapping

import pytest

from testrange.exceptions import (
    DanglingDependencyError,
    DuplicateNodeError,
    GraphCycleError,
    GraphError,
    PlanError,
    SelfDependencyError,
)
from testrange.graph import (
    BuildGraph,
    Cacheability,
    Edge,
    EdgeKind,
    Node,
    NodeContext,
)


class _StubNode(Node):
    """A minimal concrete node for exercising the graph algorithms.

    ``cache_key`` folds the supplied dependency keys deterministically so the
    same stub can later back DAG-5 transitive-key tests; for DAG-2 it only needs
    to be a pure function.
    """

    def __init__(self, name: str, kind: str = "stub") -> None:
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
        del ctx  # the stub's key is pure declaration
        material = self._name
        for dep in sorted(dependency_keys):
            material += f"|{dep}={dependency_keys[dep]}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def materialize(self, ctx: NodeContext) -> None:
        self.calls.append("materialize")

    def realize(self, ctx: NodeContext) -> None:
        self.calls.append("realize")


class _EmptyContext:
    """Satisfies the (empty) NodeContext protocol for signature-level tests."""


def _names(nodes: tuple[Node, ...]) -> list[str]:
    return [n.name for n in nodes]


def _wave_names(graph: BuildGraph) -> list[list[str]]:
    return [_names(wave) for wave in graph.waves()]


# --------------------------------------------------------------------------
# Edge
# --------------------------------------------------------------------------


class TestEdge:
    def test_defaults_are_ordering(self) -> None:
        e = Edge("web", "db")
        assert e.kind is EdgeKind.ORDERING
        assert e.cacheability is Cacheability.ORDERING
        assert e.affects_cache_key is False

    @pytest.mark.parametrize(
        ("cacheability", "expected"),
        [
            (Cacheability.ORDERING, False),
            (Cacheability.BAKE, True),
            (Cacheability.REPLAY, True),
        ],
    )
    def test_affects_cache_key(self, cacheability: Cacheability, expected: bool) -> None:
        assert Edge("b", "a", cacheability=cacheability).affects_cache_key is expected

    @pytest.mark.parametrize(("dependent", "dependency"), [("", "a"), ("b", ""), ("", "")])
    def test_empty_endpoint_rejected(self, dependent: str, dependency: str) -> None:
        with pytest.raises(ValueError, match="non-empty node name"):
            Edge(dependent, dependency)

    def test_is_frozen_hashable(self) -> None:
        # Frozen dataclass: usable as a dict/set key (edges go into tuples).
        assert hash(Edge("b", "a")) == hash(Edge("b", "a"))


# --------------------------------------------------------------------------
# Node ABC
# --------------------------------------------------------------------------


class TestNode:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            Node()  # type: ignore[abstract]

    def test_stub_implements_contract(self) -> None:
        n = _StubNode("db", kind="vm")
        assert n.name == "db"
        assert n.kind == "vm"
        ctx = _EmptyContext()
        assert n.cache_key(ctx, {}) == n.cache_key(ctx, {})  # pure

    def test_cache_key_folds_dependency_keys(self) -> None:
        n = _StubNode("web")
        ctx = _EmptyContext()
        assert n.cache_key(ctx, {}) != n.cache_key(ctx, {"db": "deadbeef"})

    def test_hooks_accept_a_context_and_run(self) -> None:
        # The hooks take a NodeContext (an empty Protocol until DAG-6 widens it),
        # so any object stands in. Calling them records the call on the stub.
        n = _StubNode("db")
        ctx: NodeContext = object()  # structurally satisfies the empty protocol
        n.materialize(ctx)
        n.realize(ctx)
        assert n.calls == ["materialize", "realize"]


# --------------------------------------------------------------------------
# BuildGraph construction / validation
# --------------------------------------------------------------------------


class TestBuildGraphValidation:
    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            BuildGraph("", [_StubNode("a")])

    def test_empty_graph_ok(self) -> None:
        g = BuildGraph("empty", [])
        assert len(g) == 0
        assert g.waves() == ()
        assert g.topological_order() == ()

    def test_single_node_no_edges(self) -> None:
        g = BuildGraph("one", [_StubNode("a")])
        assert _wave_names(g) == [["a"]]

    def test_duplicate_node_name(self) -> None:
        with pytest.raises(DuplicateNodeError, match="two nodes named 'a'"):
            BuildGraph("dup", [_StubNode("a"), _StubNode("a")])

    def test_dangling_dependency(self) -> None:
        with pytest.raises(DanglingDependencyError, match="unknown node 'z'"):
            BuildGraph("dangle", [_StubNode("a")], [Edge("a", "z")])

    def test_dangling_dependent(self) -> None:
        with pytest.raises(DanglingDependencyError, match="unknown node 'z'"):
            BuildGraph("dangle", [_StubNode("a")], [Edge("z", "a")])

    def test_self_dependency(self) -> None:
        with pytest.raises(SelfDependencyError, match="depends on itself"):
            BuildGraph("self", [_StubNode("a")], [Edge("a", "a")])

    @pytest.mark.parametrize(
        ("nodes", "edges", "cycle_members"),
        [
            (["a", "b"], [("a", "b"), ("b", "a")], {"a", "b"}),
            (["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")], {"a", "b", "c"}),
            # A cycle buried among acyclic nodes is still caught.
            (["a", "b", "c", "d"], [("b", "a"), ("c", "d"), ("d", "c")], {"c", "d"}),
            # Longer cycle, with an acyclic prefix (b<-a, then e->d->c->e downstream).
            (
                ["a", "b", "c", "d", "e"],
                [("b", "a"), ("c", "b"), ("d", "c"), ("e", "d"), ("c", "e")],
                {"c", "d", "e"},
            ),
        ],
    )
    def test_cycle_detected(
        self, nodes: list[str], edges: list[tuple[str, str]], cycle_members: set[str]
    ) -> None:
        with pytest.raises(GraphCycleError, match="cycle") as exc:
            BuildGraph("cyc", [_StubNode(n) for n in nodes], [Edge(d, s) for d, s in edges])
        path = self._reported_path(str(exc.value))
        # The reported path is a closed chain of exactly the cycle's members...
        assert path[0] == path[-1]
        assert set(path) == cycle_members
        # ...and every consecutive hop is a real dependency edge (pins the slice math).
        edge_set = set(edges)
        for first, second in itertools.pairwise(path):
            assert (first, second) in edge_set

    @staticmethod
    def _reported_path(message: str) -> list[str]:
        body = message.split("cycle:", 1)[1].split("(", 1)[0]
        return [tok.strip() for tok in body.split("->")]

    def test_cycle_path_deterministic_under_edge_reordering(self) -> None:
        # Same nodes, edges declared in two orders -> identical reported path.
        # (Root selection keys off node order, so node order is held fixed; within
        # a node the dependency edges are sorted, so edge order cannot matter.)
        nodes = [_StubNode(n) for n in ("a", "b", "c")]
        edges = [Edge("a", "b"), Edge("b", "c"), Edge("c", "a")]
        with pytest.raises(GraphCycleError) as e1:
            BuildGraph("c1", nodes, edges)
        with pytest.raises(GraphCycleError) as e2:
            BuildGraph("c2", nodes, list(reversed(edges)))
        assert self._reported_path(str(e1.value)) == self._reported_path(str(e2.value))

    def test_deep_cycle_reports_cleanly_not_recursionerror(self) -> None:
        # A pathologically deep cycle must raise GraphCycleError (a PlanError),
        # never RecursionError — the cycle finder is iterative, not recursive.
        n = 1500
        names = [f"p{i}" for i in range(n)]
        edges = [Edge(names[i], names[(i + 1) % n]) for i in range(n)]
        with pytest.raises(GraphCycleError):
            BuildGraph("deep", [_StubNode(x) for x in names], edges)

    def test_graph_errors_are_plan_errors(self) -> None:
        # DAG-13: graph validation flows through the invalid-plan exit-code path.
        for err in (
            DuplicateNodeError,
            DanglingDependencyError,
            SelfDependencyError,
            GraphCycleError,
        ):
            assert issubclass(err, GraphError)
            assert issubclass(err, PlanError)


# --------------------------------------------------------------------------
# BuildGraph topology
# --------------------------------------------------------------------------


class TestBuildGraphTopology:
    def test_linear_chain(self) -> None:
        # c needs b needs a  ->  three single-node waves, a first.
        g = BuildGraph(
            "chain",
            [_StubNode("c"), _StubNode("b"), _StubNode("a")],
            [Edge("c", "b"), Edge("b", "a")],
        )
        assert _wave_names(g) == [["a"], ["b"], ["c"]]
        assert _names(g.topological_order()) == ["a", "b", "c"]
        assert _names(g.reverse_topological_order()) == ["c", "b", "a"]

    def test_independent_nodes_one_wave_sorted(self) -> None:
        g = BuildGraph("indep", [_StubNode("z"), _StubNode("a"), _StubNode("m")])
        # Single wave, deterministically sorted by name.
        assert _wave_names(g) == [["a", "m", "z"]]

    def test_diamond(self) -> None:
        # d needs b,c ; b,c need a.
        g = BuildGraph(
            "diamond",
            [_StubNode(n) for n in ("a", "b", "c", "d")],
            [Edge("b", "a"), Edge("c", "a"), Edge("d", "b"), Edge("d", "c")],
        )
        assert _wave_names(g) == [["a"], ["b", "c"], ["d"]]

    def test_dependency_spanning_multiple_waves(self) -> None:
        # c needs a and b; b needs a. c must wait until b's wave, not a's.
        g = BuildGraph(
            "span",
            [_StubNode(n) for n in ("a", "b", "c")],
            [Edge("c", "a"), Edge("c", "b"), Edge("b", "a")],
        )
        assert _wave_names(g) == [["a"], ["b"], ["c"]]

    def test_two_roots_join(self) -> None:
        # web needs db and cache (two independent roots).
        g = BuildGraph(
            "join",
            [_StubNode(n) for n in ("db", "cache", "web")],
            [Edge("web", "db"), Edge("web", "cache")],
        )
        assert _wave_names(g) == [["cache", "db"], ["web"]]

    def test_duplicate_edge_does_not_double_count(self) -> None:
        # Two identical b->a edges (e.g. inferred infra + explicit .needs()).
        g = BuildGraph(
            "dupedge", [_StubNode("a"), _StubNode("b")], [Edge("b", "a"), Edge("b", "a")]
        )
        assert _wave_names(g) == [["a"], ["b"]]
        assert _names(g.dependencies_of("b")) == ["a"]

    def test_disconnected_components_same_depth(self) -> None:
        # Two independent chains b->a and y->x share waves by depth.
        g = BuildGraph(
            "multi",
            [_StubNode(n) for n in ("a", "b", "x", "y")],
            [Edge("b", "a"), Edge("y", "x")],
        )
        assert _wave_names(g) == [["a", "x"], ["b", "y"]]

    def test_disconnected_components_different_depths(self) -> None:
        # A depth-3 chain (r3<-r2<-r1) alongside a lone node `solo`: the shallow
        # component finishes in wave 0 while the deep one keeps producing waves.
        g = BuildGraph(
            "depths",
            [_StubNode(n) for n in ("r1", "r2", "r3", "solo")],
            [Edge("r2", "r1"), Edge("r3", "r2")],
        )
        assert _wave_names(g) == [["r1", "solo"], ["r2"], ["r3"]]

    def test_waves_deterministic_under_input_reordering(self) -> None:
        # The same topology declared with nodes and edges in shuffled order
        # produces byte-identical waves and topological order.
        edges = [Edge("b", "a"), Edge("c", "a"), Edge("d", "b"), Edge("d", "c")]
        g1 = BuildGraph("o1", [_StubNode(n) for n in ("a", "b", "c", "d")], edges)
        g2 = BuildGraph(
            "o2",
            [_StubNode(n) for n in ("d", "c", "b", "a")],
            list(reversed(edges)),
        )
        assert _wave_names(g1) == _wave_names(g2) == [["a"], ["b", "c"], ["d"]]
        assert _names(g1.topological_order()) == _names(g2.topological_order())

    def test_reverse_topological_order_on_diamond(self) -> None:
        # Teardown order on a non-linear graph: dependents before dependencies.
        g = BuildGraph(
            "diamond",
            [_StubNode(n) for n in ("a", "b", "c", "d")],
            [Edge("b", "a"), Edge("c", "a"), Edge("d", "b"), Edge("d", "c")],
        )
        assert _names(g.reverse_topological_order()) == ["d", "c", "b", "a"]

    def test_waves_are_cached_identity(self) -> None:
        # waves() returns the precomputed tuple (immutable), not a fresh recompute.
        g = BuildGraph("stable", [_StubNode("a"), _StubNode("b")], [Edge("b", "a")])
        assert g.waves() is g.waves()


# --------------------------------------------------------------------------
# BuildGraph read surface
# --------------------------------------------------------------------------


class TestBuildGraphAccessors:
    def _diamond(self) -> BuildGraph:
        return BuildGraph(
            "d",
            [_StubNode(n) for n in ("a", "b", "c", "d")],
            [Edge("b", "a"), Edge("c", "a"), Edge("d", "b"), Edge("d", "c")],
        )

    def test_node_lookup(self) -> None:
        g = self._diamond()
        assert g.node("b").name == "b"
        with pytest.raises(KeyError):
            g.node("nope")

    def test_contains_and_len_and_iter(self) -> None:
        g = self._diamond()
        assert "a" in g
        assert "nope" not in g
        assert len(g) == 4
        assert _names(tuple(g)) == ["a", "b", "c", "d"]
        assert g.names == ("a", "b", "c", "d")

    def test_dependencies_and_dependents(self) -> None:
        g = self._diamond()
        assert _names(g.dependencies_of("d")) == ["b", "c"]
        assert _names(g.dependencies_of("a")) == []
        assert _names(g.dependents_of("a")) == ["b", "c"]
        assert _names(g.dependents_of("d")) == []

    def test_middle_node_is_both_dependency_and_dependent(self) -> None:
        # b<-a, {c,d}<-b: b depends on a AND is depended on by c and d.
        g = BuildGraph(
            "mid",
            [_StubNode(n) for n in ("a", "b", "c", "d")],
            [Edge("b", "a"), Edge("c", "b"), Edge("d", "b")],
        )
        assert _names(g.dependencies_of("b")) == ["a"]
        assert _names(g.dependents_of("b")) == ["c", "d"]

    def test_accessors_raise_keyerror_on_unknown(self) -> None:
        g = self._diamond()
        with pytest.raises(KeyError):
            g.dependencies_of("nope")
        with pytest.raises(KeyError):
            g.dependents_of("nope")

    def test_repr(self) -> None:
        g = self._diamond()
        r = repr(g)
        assert "BuildGraph" in r and "nodes=4" in r and "edges=4" in r

    def test_graph_is_immutable(self) -> None:
        # The high-value integrity guarantee: a finalized graph cannot be
        # mutated, so the public tuples can never drift from the indices.
        g = self._diamond()
        for attr, value in (("nodes", ()), ("edges", ()), ("name", "x"), ("_waves", ())):
            with pytest.raises(AttributeError, match="immutable"):
                setattr(g, attr, value)
        # And the public surface is unchanged after the rejected writes.
        assert len(g) == 4
        assert _names(g.topological_order()) == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------
# Forward-compat seam evidence (early DAG-19/DAG-20 evidence)
# --------------------------------------------------------------------------


class TestForwardCompatSeams:
    """The graph admits new node kinds and non-ordering edge cacheabilities
    without any change to the algorithms — topology keys only off endpoints."""

    def test_new_node_kind_accepted(self) -> None:
        # A deferred-style kind (e.g. an appliance) is just another Node subclass
        # with a different `kind` tag; the graph treats it identically.
        appliance = _StubNode("vcenter", kind="appliance")
        esxi = _StubNode("esxi", kind="hypervisor")
        g = BuildGraph("future", [esxi, appliance], [Edge("vcenter", "esxi")])
        assert _wave_names(g) == [["esxi"], ["vcenter"]]
        assert g.node("vcenter").kind == "appliance"

    def test_bake_edge_accepted_and_topology_unchanged(self) -> None:
        # A future cacheable (bake) edge orders identically to an ordering edge;
        # only its cache participation differs (consumed by DAG-5, not topology).
        ordering = BuildGraph(
            "ord",
            [_StubNode("a"), _StubNode("b")],
            [Edge("b", "a", cacheability=Cacheability.ORDERING)],
        )
        bake = BuildGraph(
            "bake",
            [_StubNode("a"), _StubNode("b")],
            [Edge("b", "a", cacheability=Cacheability.BAKE)],
        )
        assert _wave_names(ordering) == _wave_names(bake) == [["a"], ["b"]]
        assert ordering.edges[0].affects_cache_key is False
        assert bake.edges[0].affects_cache_key is True
