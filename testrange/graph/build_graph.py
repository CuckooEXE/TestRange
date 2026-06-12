"""The :class:`BuildGraph` — a frozen, validated dependency DAG.

This is the single interface the 2.0 executor consumes (ADR-0030). It is built
from a set of :class:`~testrange.graph.node.Node` objects and a set of
:class:`~testrange.graph.edge.Edge` dependencies, and at construction it:

- rejects **duplicate node names** (names are identities),
- rejects **dangling dependencies** (an edge referencing an unknown node),
- rejects **self-dependencies** (an edge from a node to itself),
- rejects **cycles** (and reports a concrete cycle path),

then precomputes the **topological waves** — each wave a set of nodes whose
dependencies are all satisfied by earlier waves. The executor dispatches a wave
onto the bounded I/O pool in parallel and gates the next wave on it; teardown
walks the waves in reverse. Ordering within a wave is deterministic (by name) so
``testrange graph --order`` and the executor schedule reproducibly.

The graph is *kind-agnostic*: topology depends only on edge endpoints, never on
:class:`~testrange.graph.edge.EdgeKind` or
:class:`~testrange.graph.edge.Cacheability`. That is what lets post-MVP edge and
node kinds drop in without reshaping this type (DAG-19/DAG-20).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from testrange.exceptions import (
    DanglingDependencyError,
    DuplicateNodeError,
    GraphCycleError,
    SelfDependencyError,
)
from testrange.graph.edge import Edge
from testrange.graph.node import Node


class BuildGraph:
    """A frozen, validated dependency DAG of :class:`Node` objects.

    Construct with a name, the nodes, and the dependency edges; construction
    validates and precomputes the topological waves. The instance is genuinely
    immutable — :meth:`__setattr__` rejects every write once construction
    finishes, so the public ``nodes``/``edges`` tuples can never drift from the
    precomputed indices. Treat it as a value.
    """

    name: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    _by_name: dict[str, Node]
    _deps: dict[str, frozenset[str]]
    _dependents: dict[str, frozenset[str]]
    _waves: tuple[tuple[Node, ...], ...]
    _content_waves: tuple[tuple[Node, ...], ...]
    _frozen: bool = False

    def __setattr__(self, name: str, value: object) -> None:
        # Real immutability, not convention: every attribute is set during
        # __init__ (while ``_frozen`` is still the class default ``False``), then
        # the final ``self._frozen = True`` seals the instance and any later
        # write — public tuple or private index — raises.
        if self._frozen:
            raise AttributeError(
                f"BuildGraph is immutable; cannot reassign {name!r} after construction"
            )
        object.__setattr__(self, name, value)

    def __init__(self, name: str, nodes: Sequence[Node], edges: Sequence[Edge] = ()) -> None:
        if not name:
            raise ValueError("BuildGraph.name must be a non-empty string")

        node_tuple = tuple(nodes)
        edge_tuple = tuple(edges)

        by_name: dict[str, Node] = {}
        for node in node_tuple:
            if node.name in by_name:
                raise DuplicateNodeError(
                    f"build graph {name!r} has two nodes named {node.name!r}; "
                    "node names are identities and must be unique"
                )
            by_name[node.name] = node

        # Dependency adjacency, deduped by endpoint pair: topology is a relation,
        # not a multigraph — a node depended on twice (e.g. an inferred infra edge
        # plus an explicit ``.needs()``) is still one predecessor. The full edge
        # tuple is retained for the cacheability-aware transitive-key walk (DAG-5).
        # Policy (decided in DAG-5): two edges with the *same* endpoints but
        # conflicting ``cacheability`` collapse strongest-wins — the dependency
        # folds into the dependent's key iff ANY connecting edge is cacheable
        # (see ``content_dependencies_of``). An ordering edge added next to a
        # bake edge is not a contradiction; it is a weaker statement the
        # stronger one subsumes.
        deps: dict[str, set[str]] = {n: set() for n in by_name}
        dependents: dict[str, set[str]] = {n: set() for n in by_name}
        for edge in edge_tuple:
            if edge.dependent == edge.dependency:
                raise SelfDependencyError(
                    f"build graph {name!r}: node {edge.dependent!r} depends on itself"
                )
            for endpoint, role in ((edge.dependent, "dependent"), (edge.dependency, "dependency")):
                if endpoint not in by_name:
                    raise DanglingDependencyError(
                        f"build graph {name!r}: edge {edge.dependent!r} -> {edge.dependency!r} "
                        f"names unknown node {endpoint!r} as its {role}; "
                        f"known nodes: {sorted(by_name)}"
                    )
            deps[edge.dependent].add(edge.dependency)
            dependents[edge.dependency].add(edge.dependent)

        self.name = name
        self.nodes = node_tuple
        self.edges = edge_tuple
        self._by_name = by_name
        self._deps = {n: frozenset(d) for n, d in deps.items()}
        self._dependents = {n: frozenset(d) for n, d in dependents.items()}
        # Computing the waves doubles as cycle detection: a cycle leaves some
        # nodes never reaching in-degree zero, so they are absent from the waves.
        self._waves = self._compute_waves(self._deps, self._dependents)
        # Materialize gating uses the *content* sub-DAG (cacheable edges only,
        # strongest-wins per endpoint pair): placement (ordering) sequences the
        # run, not the build, so MVP builds — whose graphs carry only ordering
        # edges — stay fully concurrent (the v0 behavior). A sub-relation of an
        # acyclic relation is acyclic, so this can never raise.
        content_deps: dict[str, set[str]] = {n: set() for n in by_name}
        content_dependents: dict[str, set[str]] = {n: set() for n in by_name}
        for edge in edge_tuple:
            if edge.affects_cache_key:
                content_deps[edge.dependent].add(edge.dependency)
                content_dependents[edge.dependency].add(edge.dependent)
        self._content_waves = self._compute_waves(
            {n: frozenset(d) for n, d in content_deps.items()},
            {n: frozenset(d) for n, d in content_dependents.items()},
        )
        self._frozen = True  # seal: no attribute may be reassigned past here

    def _compute_waves(
        self,
        deps: dict[str, frozenset[str]],
        dependents: dict[str, frozenset[str]],
    ) -> tuple[tuple[Node, ...], ...]:
        """Kahn's algorithm by levels; raises :class:`GraphCycleError` on a cycle.

        A wave is every node whose dependencies (under the given relation) are
        all in an earlier wave. Within a wave nodes are ordered by name for
        determinism.
        """
        done: set[str] = set()
        waves: list[tuple[Node, ...]] = []

        current = sorted(name for name, d in deps.items() if not d)
        while current:
            waves.append(tuple(self._by_name[n] for n in current))
            done.update(current)
            nxt: set[str] = set()
            for finished in current:
                for dependent in dependents[finished]:
                    if dependent not in done and deps[dependent] <= done:
                        nxt.add(dependent)
            current = sorted(nxt)

        if len(done) != len(self.nodes):
            cycle = self._find_cycle(done)
            raise GraphCycleError(
                f"build graph {self.name!r} has a dependency cycle: "
                + " -> ".join(cycle)
                + " (a node cannot transitively depend on itself)"
            )
        return tuple(waves)

    def _find_cycle(self, settled: set[str]) -> list[str]:
        """Return one concrete cycle (names) among the nodes not in ``settled``.

        Only called on the error path. Iterative (explicit-stack) DFS with
        grey/black coloring over the unsettled sub-DAG; the back-edge target
        closes the reported cycle. Iterative — not recursive — so a pathologically
        deep cycle reports a clean :class:`GraphCycleError` instead of blowing
        Python's recursion limit and escaping the ``PlanError`` contract.
        """
        black: set[str] = set()
        for root in self._by_name:
            if root in settled or root in black:
                continue
            # Each frame is (node, iterator over its sorted dependencies). ``path``
            # mirrors the live frame stack; ``on_path`` is its membership set for
            # O(1) back-edge detection.
            frames: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(self._deps[root])))]
            path: list[str] = [root]
            on_path: set[str] = {root}
            while frames:
                node, deps = frames[-1]
                descended = False
                for dep in deps:
                    if dep in settled or dep in black:
                        continue
                    if dep in on_path:
                        return [*path[path.index(dep) :], dep]
                    frames.append((dep, iter(sorted(self._deps[dep]))))
                    path.append(dep)
                    on_path.add(dep)
                    descended = True
                    break
                if not descended:
                    frames.pop()
                    path.pop()
                    on_path.discard(node)
                    black.add(node)
        raise AssertionError(  # pragma: no cover — unsettled nodes must contain a cycle
            f"build graph {self.name!r}: cycle detection found unsettled nodes but no cycle"
        )

    def node(self, name: str) -> Node:
        """The node named *name*, or raise :class:`KeyError`."""
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        """Membership by node *name* (mirrors :meth:`node` / :attr:`names`)."""
        return name in self._by_name

    def __iter__(self) -> Iterator[Node]:
        """Iterate nodes in declaration order."""
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def names(self) -> tuple[str, ...]:
        """Node names in declaration order."""
        return tuple(n.name for n in self.nodes)

    def dependencies_of(self, name: str) -> tuple[Node, ...]:
        """The nodes *name* directly depends on (its predecessors), sorted by name."""
        return tuple(self._by_name[d] for d in sorted(self._deps[name]))

    def content_dependencies_of(self, name: str) -> tuple[Node, ...]:
        """The dependencies whose keys fold into *name*'s cache key, sorted by name.

        A dependency is a *content* dependency when any edge connecting the
        pair is cacheable (:attr:`~testrange.graph.edge.Edge.affects_cache_key`)
        — strongest-wins over parallel edges, so an ordering edge alongside a
        bake edge never weakens invalidation. Ordering-only dependencies are
        excluded: placement is not invalidation (ADR-0030). MVP graphs emit
        only ordering edges, so this is empty everywhere and every node's key
        is its own-inputs hash.
        """
        content = {e.dependency for e in self.edges if e.dependent == name and e.affects_cache_key}
        return tuple(self._by_name[d] for d in sorted(content))

    def dependents_of(self, name: str) -> tuple[Node, ...]:
        """The nodes that directly depend on *name* (its successors), sorted by name."""
        return tuple(self._by_name[d] for d in sorted(self._dependents[name]))

    def waves(self) -> tuple[tuple[Node, ...], ...]:
        """The topological execution waves (every edge gates).

        Wave ``i`` is every node whose dependencies are all in waves ``< i``;
        the executor runs a wave's nodes in parallel and gates the next wave on
        it. Nodes within a wave are ordered by name. This is the *realize*
        schedule — ordering edges exist to sequence bring-up.
        """
        return self._waves

    def content_waves(self) -> tuple[tuple[Node, ...], ...]:
        """The waves under the content (cacheable-edge) sub-DAG.

        The *materialize* schedule: a build must wait only for dependencies
        whose output identity folds into its disk (``bake``/``replay`` edges);
        ordering edges sequence the run, not the build. MVP graphs carry only
        ordering edges, so this is a single wave and every node builds
        concurrently — the v0 behavior, preserved by construction.
        """
        return self._content_waves

    def topological_order(self) -> tuple[Node, ...]:
        """All nodes in a deterministic dependency-respecting order (waves flattened)."""
        return tuple(node for wave in self._waves for node in wave)

    def reverse_topological_order(self) -> tuple[Node, ...]:
        """Topological order reversed — the teardown order (dependents before deps)."""
        return tuple(reversed(self.topological_order()))

    def __repr__(self) -> str:
        return (
            f"BuildGraph(name={self.name!r}, nodes={len(self.nodes)}, "
            f"edges={len(self.edges)}, waves={len(self._waves)})"
        )


__all__ = ["BuildGraph"]
