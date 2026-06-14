"""The per-node transitive cache-key walk (ADR-0030, DAG-5).

Generalizes the v0 per-build ``builder.config_hash`` to every node: a node's
key is ``hash(its own inputs + the keys of its content dependencies)``, where a
*content* dependency is one reached by a cacheable (bake/replay) edge —
ordering edges contribute nothing, because a node that merely runs after
another is placed, not invalidated.

The walk runs in topological order so every dependency's key exists before a
dependent asks for it, and it is pure graph machinery: nodes do their own
hashing (:meth:`~testrange.graph.node.Node.cache_key`), the walk only routes
already-computed upstream keys along cacheable edges
(:meth:`~testrange.graph.build_graph.BuildGraph.content_dependencies_of`,
strongest-wins over parallel edges).

MVP graphs carry only ordering edges, so every ``dependency_keys`` mapping is
empty and each node's key equals its own-inputs hash — for a VM, byte-identical
to the v0 ``config_hash`` (regression-pinned in the unit suite). The machinery
exists from day one so post-MVP ``bake`` edges fold in without reshaping
anything.
"""

from __future__ import annotations

from testrange.graph.build_graph import BuildGraph
from testrange.graph.node import NodeContext


def compute_cache_keys(graph: BuildGraph, ctx: NodeContext) -> dict[str, str]:
    """Compute every node's content-addressed key, in topological order.

    Returns ``{node name: key}`` for the whole graph. Deliberately serial: VM
    nodes commonly share one base image, and a serial walk resolves it into the
    local cache once instead of racing parallel fetches of the same
    content-addressed staging path (the same reason the v0 probe was serial).
    """
    keys: dict[str, str] = {}
    for node in graph.topological_order():
        dependency_keys = {
            dep.name: keys[dep.name] for dep in graph.content_dependencies_of(node.name)
        }
        keys[node.name] = node.cache_key(ctx, dependency_keys)
    return keys


__all__ = ["compute_cache_keys"]
