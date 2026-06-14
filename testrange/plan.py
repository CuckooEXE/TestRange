"""The top-level ``Plan`` declaration: name + Hypervisor -> frozen build graph.

``Plan(name, hyp)`` is the 2.0 finalizer (ADR-0030): it validates the
declared topology, seals the mutable :class:`~testrange.hypervisor.Hypervisor`
container against further registration, and assembles the validated
:class:`~testrange.graph.build_graph.BuildGraph` the executor walks. Graph
defects (duplicate node names, dangling handle references, cycles from
``.needs()``) raise :class:`~testrange.exceptions.GraphError` — a
``PlanError`` — right here at construction, so a plan file that imports
cleanly carries a structurally-sound graph.
"""

from __future__ import annotations

from testrange.graph.build_graph import BuildGraph
from testrange.hypervisor import Hypervisor
from testrange.networks.validate import validate_hypervisor_plan
from testrange.nodes import assemble_graph


class Plan:
    """The top-level declaration: a name plus one finalized Hypervisor."""

    def __init__(self, name: str, hypervisor: Hypervisor) -> None:
        # The plan name namespaces every derived resource: stable MACs
        # (compose_mac), backend resource names, and build cache keys. An
        # unnamed plan would silently share that namespace with any other
        # unnamed plan, so it leads as a required positional rather than
        # defaulting.
        if not name:
            raise ValueError(
                "Plan(name, ...) requires a non-empty name; it namespaces stable "
                "MACs, backend resource names, and the build cache"
            )
        # User-facing trust boundary: catch a v0-style call shape or a stray
        # value before it becomes an opaque attribute error downstream.
        if not isinstance(hypervisor, Hypervisor):
            raise TypeError(
                f"Plan(name, hypervisor) takes a Hypervisor, got {type(hypervisor).__name__}"
            )
        validate_hypervisor_plan(
            hypervisor.declared_switches,
            hypervisor.declared_pools,
            hypervisor.declared_vms,
        )
        hypervisor.freeze()
        self._name = name
        self._hypervisor = hypervisor
        self._graph = assemble_graph(name, hypervisor)

    @property
    def name(self) -> str:
        return self._name

    @property
    def hypervisor(self) -> Hypervisor:
        """The finalized (frozen) topology container."""
        return self._hypervisor

    @property
    def graph(self) -> BuildGraph:
        """The frozen, validated dependency DAG the executor consumes."""
        return self._graph

    def __repr__(self) -> str:
        return f"Plan(name={self._name!r}, graph={self._graph!r})"


__all__ = ["Plan"]
