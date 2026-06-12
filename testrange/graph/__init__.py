"""The TestRange 2.0 build graph — the pure dependency-DAG model (ADR-0030).

A plan is a *dependency graph*, not a flat list under one host. This package is
the backend-free core of that model:

- :class:`Node` — a kinded unit of materializable state (identity, cache-key
  contract, materialize/realize hooks), over the executor-supplied
  :class:`NodeContext`.
- :class:`Edge` — a directed dependency ``dependent -> dependency`` with an
  :class:`EdgeKind` and a :class:`Cacheability`.
- :class:`BuildGraph` — a frozen, validated DAG: topo-sort into waves, cycle
  detection, dangling-dependency / duplicate-name / self-edge checks. The
  executor consumes only this.

The concrete node kinds, the imperative builder surface, the transitive cache
key, and the executor build on this layer (DAG-3..DAG-7); nothing here imports a
driver or the orchestrator.
"""

from __future__ import annotations

from testrange.graph.build_graph import BuildGraph
from testrange.graph.edge import Cacheability, Edge, EdgeKind
from testrange.graph.keys import compute_cache_keys
from testrange.graph.node import Node, NodeContext

__all__ = [
    "BuildGraph",
    "Cacheability",
    "Edge",
    "EdgeKind",
    "Node",
    "NodeContext",
    "compute_cache_keys",
]
