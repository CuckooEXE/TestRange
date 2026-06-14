"""Typed plan-construction handles (ADR-0030, DAG-3/DAG-4).

A handle is what :meth:`Hypervisor.add_pool` / :meth:`~Hypervisor.add_switch` /
:meth:`~Hypervisor.add_vm` return, and the only sanctioned way for a device or
an edge to reference a registered node: :class:`~testrange.devices.OSDrive`
takes a :class:`PoolHandle`, :class:`~testrange.devices.NetworkIface` takes a
:class:`NetworkHandle`, :meth:`VMHandle.needs` takes handles. A miswire — a
network where a pool belongs — is a *type* error under mypy and a ``TypeError``
at construction; a bad name is a loud ``KeyError`` from the registry mapping
(``hyp.pools["pool1"]``) at plan-construction time.

Each handle subclasses :class:`str` and *is* the plan-level name. That is a
deliberate contract, not a convenience: everything downstream of plan
construction — builders rendering netplan, the sidecar's dnsmasq records, the
driver's ``network_refs`` keying, the per-build ``config_hash`` — consumes the
plan-level *name*, and a handle that is the name keeps every one of those
consumers (and the v0-compatible cache keys, DAG-5) byte-identical. The
subclass carries what the name alone cannot: the handle's *kind* (so mypy can
reject a miswire) and, per kind, the wiring needed to infer the implicit infra
edges (a network's owning switch) or register explicit ones (``.needs()``).
"""

from __future__ import annotations

from typing import Protocol


class EdgeSink(Protocol):
    """Where a handle's ``.needs()`` records an explicit ordering edge.

    Structurally satisfied by :class:`~testrange.hypervisor.Hypervisor`; the
    protocol exists so this module never imports it (the dependency arrow
    points handle -> container, not both ways).
    """

    def add_explicit_edge(self, dependent: str, dependency: str) -> None: ...


class Handle(str):
    """Base of the typed handles. A handle *is* its node's plan-level name."""

    __slots__ = ()

    @property
    def node_name(self) -> str:
        """The graph-node identity this handle references (``"<kind>:<name>"``).

        Node names are kind-qualified so a pool and a VM sharing a plan-level
        name can never collide on graph identity.
        """
        raise NotImplementedError  # pragma: no cover — every concrete kind overrides

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str.__repr__(self)})"


class PoolHandle(Handle):
    """A registered storage pool, as returned by ``hyp.add_pool(...)``."""

    __slots__ = ()

    def __new__(cls, name: str) -> PoolHandle:
        if not name:
            raise ValueError("PoolHandle requires a non-empty pool name")
        return super().__new__(cls, name)

    @property
    def node_name(self) -> str:
        return f"pool:{self}"


class SwitchHandle(Handle):
    """A registered L2 switch, as returned by ``hyp.add_switch(...)``."""

    __slots__ = ()

    def __new__(cls, name: str) -> SwitchHandle:
        if not name:
            raise ValueError("SwitchHandle requires a non-empty switch name")
        return super().__new__(cls, name)

    @property
    def node_name(self) -> str:
        # A switch and its bindable networks realize as ONE graph node (the
        # network node carries the switch fabric + sidecar), so the switch
        # handle resolves to that node.
        return f"network:{self}"


class NetworkHandle(Handle):
    """A bindable network on a registered switch (``hyp.networks["netA"]``).

    Carries :attr:`switch` — the owning switch's name — because the network's
    graph node is the *switch* unit (fabric + networks + sidecar realize
    together); a NIC referencing this handle gives its VM an inferred edge onto
    that node.
    """

    # str is a variable-length built-in, so a non-empty __slots__ is not
    # available here; the owning-switch ref rides the instance dict instead.
    _switch: str

    def __new__(cls, name: str, *, switch: str) -> NetworkHandle:
        if not name:
            raise ValueError("NetworkHandle requires a non-empty network name")
        if not switch:
            raise ValueError(f"NetworkHandle({name!r}) requires its owning switch name")
        self = super().__new__(cls, name)
        self._switch = switch
        return self

    @property
    def switch(self) -> str:
        """The owning switch's plan-level name."""
        return self._switch

    @property
    def node_name(self) -> str:
        return f"network:{self._switch}"


class VMHandle(Handle):
    """A registered VM, as returned by ``hyp.add_vm(...)``.

    :meth:`needs` is the explicit-ordering surface: it records an ordering
    edge into the owning container, so the executor materializes/realizes the
    needed node first.
    """

    _edges: EdgeSink

    def __new__(cls, name: str, *, edges: EdgeSink) -> VMHandle:
        if not name:
            raise ValueError("VMHandle requires a non-empty VM name")
        self = super().__new__(cls, name)
        self._edges = edges
        return self

    @property
    def node_name(self) -> str:
        return f"vm:{self}"

    def needs(self, *others: Handle) -> None:
        """Declare that this VM runs only after *others* are up.

        Adds an explicit ordering edge per handle (``EdgeKind.ORDERING`` —
        sequencing only, never cache invalidation). Cycles are rejected when
        ``Plan(...)`` freezes the graph.
        """
        for other in others:
            if not isinstance(other, Handle):
                raise TypeError(
                    f"needs() takes handles returned by hyp.add_*/registries; "
                    f"got {type(other).__name__}"
                )
            if other.node_name == self.node_name:
                raise ValueError(f"VM {str(self)!r} cannot need itself")
            self._edges.add_explicit_edge(self.node_name, other.node_name)


__all__ = [
    "EdgeSink",
    "Handle",
    "NetworkHandle",
    "PoolHandle",
    "SwitchHandle",
    "VMHandle",
]
