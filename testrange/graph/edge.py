"""Edges of the build graph: a directed dependency plus its cacheability.

An :class:`Edge` is a directed dependency ``dependent -> dependency`` read as
"*dependent* needs *dependency*". The dependency is materialized/realized first;
the topological order puts it ahead of the dependent. Both endpoints are node
**names** (the node's stable identity, :attr:`~testrange.graph.node.Node.name`),
never the node objects — the :class:`~testrange.graph.build_graph.BuildGraph`
resolves names to nodes and rejects dangling references at construction.

Two orthogonal facets ride on every edge:

- :class:`EdgeKind` — the *semantic relationship* the edge encodes. The 2.0 MVP
  emits exactly one kind, :attr:`EdgeKind.ORDERING` (sequencing: "do A before
  B"). The deferred relationship kinds (``manage``, ``collect_from``) are not
  modeled here yet; they land as new members without reshaping the graph
  algorithms, which key only off the endpoints (ADR-0030, DAG-19).
- :class:`Cacheability` — how the edge participates in the dependent's
  *content-addressed cache key*. An ordering edge is sequencing only and
  contributes **nothing** to the key (placement is not invalidation); a future
  ``bake`` edge folds the dependency's output identity into the key so a changed
  dependency invalidates the dependent. The transitive-key walk that consumes
  this field is DAG-5; the field exists from day one so that walk and the
  post-MVP edge kinds drop in cleanly (ADR-0030).

The graph's *topology* (ordering, cycle detection, teardown) depends only on the
endpoints, never on ``kind`` or ``cacheability`` — that kind-agnosticism is the
forward-compatibility seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EdgeKind(Enum):
    """The semantic relationship an edge encodes.

    ``ORDERING`` is the only kind the 2.0 MVP emits. The deferred relationship
    kinds (``manage`` / ``collect_from``) are intentionally absent until the
    appliance epic builds them (DAG-19); the graph algorithms never branch on
    this field, so a new kind is additive.
    """

    ORDERING = "ordering"


class Cacheability(Enum):
    """How an edge contributes to the *dependent's* content-addressed key.

    - ``ORDERING`` — sequencing only; the dependency's identity is **excluded**
      from the dependent's cache key (placement is not invalidation). Every MVP
      edge is this.
    - ``BAKE`` — the dependency's output identity is baked into the dependent's
      disk, so its content key folds into the dependent's transitive key and a
      changed dependency invalidates the dependent. Reserved for the post-MVP
      relationship edges (DAG-19); no MVP edge emits it.
    - ``REPLAY`` — a relationship whose effect is re-applied at realize time
      rather than baked into the disk. Reserved alongside ``BAKE`` (DAG-19).
    """

    ORDERING = "ordering"
    BAKE = "bake"
    REPLAY = "replay"


@dataclass(frozen=True)
class Edge:
    """A directed dependency ``dependent -> dependency`` ("dependent needs dependency").

    ``dependent`` and ``dependency`` are node names. The dependency is ordered
    *before* the dependent. ``kind`` and ``cacheability`` default to the MVP's
    ordering semantics; they are carried for the transitive-key walk (DAG-5) and
    the deferred edge kinds (DAG-19) and never affect topology.
    """

    dependent: str
    dependency: str
    kind: EdgeKind = EdgeKind.ORDERING
    cacheability: Cacheability = Cacheability.ORDERING

    def __post_init__(self) -> None:
        if not self.dependent:
            raise ValueError("Edge.dependent must be a non-empty node name")
        if not self.dependency:
            raise ValueError("Edge.dependency must be a non-empty node name")

    @property
    def affects_cache_key(self) -> bool:
        """Whether this edge folds its dependency's identity into the dependent's key.

        ``True`` only for non-ordering cacheability (``bake``/``replay``). The
        DAG-5 transitive-key walk uses this to decide which upstream keys to
        fold in; ordering edges return ``False`` so MVP node keys match the v0
        per-build ``config_hash`` for equivalent VMs.
        """
        return self.cacheability is not Cacheability.ORDERING


__all__ = ["Cacheability", "Edge", "EdgeKind"]
