"""The DAG executor: one topological walk replaces the v0 phase pipeline.

ADR-0030 (DAG-6). The executor consumes only the frozen
:class:`~testrange.graph.build_graph.BuildGraph` and is kind-agnostic: it
dispatches each wave's nodes onto the bounded I/O pool
(:func:`~testrange.orchestrator._parallel.parallel_map`, ADR-0023 — one
shared, thread-safe driver connection, never per-worker) and gates the next
wave on it. What a node *does* lives in the node kinds; what the run *needs*
lives in the :class:`~testrange.orchestrator.context.GraphContext` every hook
receives.

Two walks, two schedules:

- :func:`materialize_graph` (``build``) walks the **content** waves — a build
  waits only for dependencies whose output identity folds into its disk, so
  MVP graphs (ordering edges only) build fully concurrently, exactly like v0.
  The shared ephemeral build infra is torn down at walk end.
- :func:`realize_graph` (``run``) walks the **full** waves — ordering edges
  exist to sequence bring-up, and a node's realize does not return until the
  node is *ready* (sidecar serving, communicator answering), so an explicit
  ``.needs()`` dependent starts only after the needed node is genuinely up.

Both walks are fronted by the serial key walk (:func:`prepare_keys`, DAG-5),
which fills ``ctx.node_keys`` / ``ctx.vm_probes``; node completion is stamped
into the state ledger (DAG-9) after each hook returns, which is what
``run --resume`` skips on. Teardown needs no walk of its own: every backend
resource was recorded in ``state.json`` *before* its create call, in wave
order, so the existing LIFO state-driven teardown already reverses the
topological order (DAG-8).
"""

from __future__ import annotations

from testrange._log import get_logger
from testrange.graph.build_graph import BuildGraph
from testrange.graph.keys import compute_cache_keys
from testrange.graph.node import Node
from testrange.orchestrator._parallel import parallel_map
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.dashboard_state import VMStage
from testrange.orchestrator.vm_build import teardown_build_infra
from testrange.state.schema import PHASE_BUILD, PHASE_RUN

_log = get_logger(__name__)


def prepare_keys(ctx: GraphContext, graph: BuildGraph) -> None:
    """Run the serial cache-key walk once (DAG-5); idempotent.

    Fills ``ctx.node_keys`` for every node and — as a deliberate side effect
    of ``VMNode.cache_key`` — ``ctx.vm_probes`` with each VM's resolved build
    inputs and cache-probe outcome. Serial because VM nodes commonly share one
    base image: a serial walk fetches it into the local cache once instead of
    racing parallel fetches of the same content-addressed staging path.
    """
    if ctx.node_keys:
        return
    ctx.node_keys.update(compute_cache_keys(graph, ctx))
    for node in graph.topological_order():
        _log.debug("node %s: cache key %s", node.name, ctx.node_keys[node.name])


def probe_misses(ctx: GraphContext, graph: BuildGraph) -> list[str]:
    """The plan-level VM names whose cached disk sets are incomplete.

    Read-only against the backend. Used by ``run --require-cache`` to fail
    fast on a miss without building (ADR-0010 §1).
    """
    prepare_keys(ctx, graph)
    return sorted(p.vm.name for p in ctx.vm_probes.values() if p.cached_paths is None)


def materialize_graph(ctx: GraphContext, graph: BuildGraph) -> None:
    """The build walk: every node's ``materialize``, content waves, cache-aware.

    Cache-hit VM nodes only ledger their disk paths; misses build on the
    shared ephemeral infra (created lazily by the first miss, torn down here
    at walk end — ADR-0010 §3). A failure propagates after the in-flight wave
    drains (``parallel_map`` is fail-fast); recorded infra is then reversed by
    the caller's state-driven teardown.
    """
    ctx.store.set_phase(PHASE_BUILD)
    prepare_keys(ctx, graph)
    try:
        for wave in graph.content_waves():
            parallel_map(
                lambda node: _run_hook(ctx, node, "materialize"),
                wave,
                jobs=ctx.jobs,
            )
    finally:
        # The build infra never survives the walk — including the failure
        # path, where v0 relied on state-driven teardown to reclaim it later;
        # reclaiming it here keeps the backend clean even under --leak/repl.
        teardown_build_infra(ctx)


def realize_graph(ctx: GraphContext, graph: BuildGraph) -> None:
    """The run walk: every node's ``realize``, full waves, readiness-gated."""
    ctx.store.set_phase(PHASE_RUN)
    for wave in graph.waves():
        parallel_map(
            lambda node: _run_hook(ctx, node, "realize"),
            wave,
            jobs=ctx.jobs,
        )


def _run_hook(ctx: GraphContext, node: Node, hook: str) -> None:
    """Run one node's hook, stamp its completion, attribute failures.

    ``parallel_map`` is fail-fast and re-raises one worker's exception; tagging
    the VM here gives the dashboard the actual culprit (with the message)
    before the walk unwinds. Completion stamps land in the state ledger
    (DAG-9) only after the hook returns — a crash mid-hook leaves the node
    unstamped, so ``--resume`` re-runs it (hooks are idempotent ensures).
    """
    try:
        if hook == "materialize":
            node.materialize(ctx)
        else:
            node.realize(ctx)
    except Exception as e:
        if node.kind == "vm":
            ctx.dashboard.set_vm_stage(_vm_plan_name(node), VMStage.FAILED, detail=str(e))
        raise
    if hook == "materialize":
        ctx.store.mark_node_materialized(node.name)
        with ctx.ledger_lock:
            ctx.materialized_nodes.add(node.name)
    else:
        ctx.store.mark_node_realized(node.name)
        with ctx.ledger_lock:
            ctx.realized_nodes.add(node.name)


def _vm_plan_name(node: Node) -> str:
    """The plan-level VM name behind a ``vm:<name>`` node (dashboard key)."""
    return node.name.removeprefix("vm:")


__all__ = [
    "materialize_graph",
    "prepare_keys",
    "probe_misses",
    "realize_graph",
]
