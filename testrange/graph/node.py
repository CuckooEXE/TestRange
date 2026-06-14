"""Nodes of the build graph: the kinded ABC and the executor-context seam.

A :class:`Node` is a unit of materializable state. It carries three things the
graph and executor need:

1. a stable **identity** (:attr:`Node.name`, unique within a graph) and a
   coarse **kind** tag (:attr:`Node.kind`) for display/dispatch;
2. a **content-addressed cache-key contract** (:meth:`Node.cache_key`) — the
   per-node generalization of the v0 ``builder.config_hash``; and
3. the **materialize / realize hooks** the executor drives.

This module is the *pure model* layer (DAG-2): it imports no driver, no
orchestrator, no backend. The hooks therefore take a :class:`NodeContext`, a
structural placeholder the DAG executor (DAG-6) widens with the concrete
accessors a node needs (driver, cache, state store, ledger). Defining it as a
``Protocol`` here keeps the dependency arrow pointing the right way: the graph
declares *what a node is handed*, and the orchestrator — the one component that
may know both the graph and the driver — supplies an object that satisfies it,
without the graph package importing anything backend-shaped.

The concrete MVP kinds (``PoolNode`` / ``NetworkNode`` / ``VMNode``) and their
hook bodies are DAG-3 / DAG-7; this module fixes only the contract they implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Protocol


class NodeContext(Protocol):
    """What the executor hands a node when it materializes or realizes it.

    Intentionally empty in the pure-model layer. The DAG executor (DAG-6)
    provides a concrete context — carrying the bound driver, cache manager,
    state store, and the lock-guarded run ledgers — and widens this protocol
    with the typed accessors nodes read. A node hook receives this object and
    never imports the orchestrator or a driver directly, so the graph model
    stays backend-free.

    Not ``runtime_checkable``: with no members an ``isinstance`` check would
    accept any object and read as meaningful when it is not. DAG-6 adds the
    decorator once the protocol has accessors worth checking.
    """


class Node(ABC):
    """A kinded unit of materializable state in a :class:`BuildGraph`.

    Subclasses are the node *kinds*. The MVP kinds wrap the existing value
    types (a storage pool, a switch+sidecar, a VM recipe); the deferred kinds
    (``ApplianceNode``, ``HypervisorNode``) are new subclasses, not a reshape of
    this ABC (ADR-0030, DAG-19/DAG-20).

    The contract is deliberately small: an identity, a kind tag, a cache-key
    function, and the two lifecycle hooks. Everything topological lives on
    :class:`BuildGraph`, which consumes nodes by :attr:`name`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """This node's identity — unique within its graph.

        Used as the graph registry key, the topological identity, and the basis
        for the deterministic backend resource name the driver composes. Two
        nodes with the same name in one graph is a construction error
        (``DuplicateNodeError``).
        """

    @property
    @abstractmethod
    def kind(self) -> str:
        """A coarse kind tag for display and dispatch (e.g. ``"pool"``, ``"vm"``).

        Human-facing (``testrange graph`` renders it) and stable. Not used by
        the graph algorithms — they are kind-agnostic — so a new kind tag never
        perturbs ordering or validation.
        """

    @abstractmethod
    def cache_key(self, ctx: NodeContext, dependency_keys: Mapping[str, str]) -> str:
        """The node's content-addressed key, given its content dependencies' keys.

        The per-node generalization of v0 ``builder.config_hash``
        (``builders/base.py``): ``hash(this node's own inputs + the keys of its
        *content* dependencies)``. ``dependency_keys`` maps a content
        dependency's node name to that dependency's already-computed key; it is
        supplied by the graph-level transitive walk
        (:func:`~testrange.graph.keys.compute_cache_keys`, DAG-5), which
        includes an upstream only when a connecting edge is cacheable
        (:attr:`~testrange.graph.edge.Edge.affects_cache_key`).

        ``ctx`` is the same executor-supplied context the lifecycle hooks get:
        a node's "own inputs" can include profile- and cache-resolved facts (a
        base image's content sha, the deterministic NIC MACs the bound driver
        composes), so key computation is contextual by design — it is why
        ``graph --cache`` needs ``--profile``. Implementations MUST be
        deterministic for a given (plan, profile, cache) triple: no run id,
        clock, or randomness.

        MVP graphs carry only ordering edges, so ``dependency_keys`` is empty
        and a node's key equals its own-inputs hash — matching the v0 key for an
        equivalent VM (no spurious cache busting).
        """

    @abstractmethod
    def materialize(self, ctx: NodeContext) -> None:
        """Build and cache this node's artifact (the *build* half of the walk).

        For a VM this drives the disk-set build + capture into the cache; for
        infra (pool/network) it is the create needed before dependents can
        build. Must be idempotent and skippable on a cache/ledger hit — the
        executor may call it on a node whose artifact already exists. Bodies are
        DAG-7 (libvirt); the executor (DAG-6) supplies ``ctx``.
        """

    @abstractmethod
    def realize(self, ctx: NodeContext) -> None:
        """Bring this node up for the run (the *realize* half of the walk).

        For a VM this drives create+start+communicator-bind+wait-ready; for
        infra it creates the pool/switch/sidecar. Must be idempotent so
        ``--resume`` and re-runs are safe. Bodies are DAG-7; ``ctx`` is the
        executor's (DAG-6).
        """


__all__ = ["Node", "NodeContext"]
