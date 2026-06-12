"""The mutable, backend-agnostic ``Hypervisor`` node container (ADR-0030, DAG-4).

The 2.0 construction surface: a plan is built *imperatively* — ``add_pool`` /
``add_switch`` / ``add_vm`` register a node and return its concrete typed
handle — then frozen by ``Plan(name, hyp)`` into the validated
:class:`~testrange.graph.build_graph.BuildGraph` the executor walks.

Every registered node is also reachable through a typed registry —
:attr:`pools` / :attr:`switches` / :attr:`networks` / :attr:`vms`, each a
``Mapping[str, <Handle>]`` — so references read ``OSDrive(hyp.pools["pool1"],
16)`` and ``hyp.vms["web"].needs(hyp.vms["db"])``. The canonical ref form is
``["name"]`` (typed return + loud ``KeyError`` at construction), deliberately
NOT attribute access: runtime-added names cannot be statically typed, so an
attribute accessor would have to type as the handle for *any* name and defeat
mypy.

This type still selects no driver and carries no connection: the backend is
supplied at run time via a connection profile (``--profile``). The concrete
scheme-marker subclasses pin a backend *scheme* only. ``build_switch`` remains
portable topology (ADR-0016): the optional transient build-phase network,
realized exactly like a run-phase one.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Generic, TypeVar

from testrange.devices.pool.base import StoragePool
from testrange.handles import NetworkHandle, PoolHandle, SwitchHandle, VMHandle
from testrange.networks.base import Switch
from testrange.vms.recipe import VMRecipe

_H = TypeVar("_H")


class _Registry(Mapping[str, _H], Generic[_H]):
    """A read-only name -> handle view with a loud, teaching ``KeyError``."""

    def __init__(self, kind: str, items: dict[str, _H]) -> None:
        self._kind = kind
        self._items = items

    def __getitem__(self, name: str) -> _H:
        try:
            return self._items[name]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "(none registered)"
            raise KeyError(f"no {self._kind} {name!r}; known: {known}") from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"<{self._kind} registry: {sorted(self._items)}>"


class Hypervisor:
    """A mutable node container: register topology, get typed handles back.

    Mutable while you build it, frozen at rest: ``Plan(name, hyp)`` seals the
    container (later ``add_*`` calls raise) and assembles the build graph.
    Registration order is preserved — it is the deterministic basis for the
    build-address slots and the sidecar's home pool (the first added pool).
    """

    def __init__(self, *, build_switch: Switch | None = None) -> None:
        self.build_switch = build_switch
        self._pools: dict[str, StoragePool] = {}
        self._switches: dict[str, Switch] = {}
        self._vms: dict[str, VMRecipe] = {}
        self._pool_handles: dict[str, PoolHandle] = {}
        self._switch_handles: dict[str, SwitchHandle] = {}
        self._network_handles: dict[str, NetworkHandle] = {}
        self._vm_handles: dict[str, VMHandle] = {}
        # Explicit ordering edges recorded by handle.needs(), as
        # (dependent node name, dependency node name) pairs in call order.
        self._explicit_edges: list[tuple[str, str]] = []
        self._frozen = False

    def _require_mutable(self, action: str) -> None:
        if self._frozen:
            raise ValueError(
                f"cannot {action}: this Hypervisor was frozen by Plan(...); "
                "declare every pool/switch/VM before finalizing the plan"
            )

    def add_pool(self, pool: StoragePool) -> PoolHandle:
        """Register a storage pool; returns its :class:`PoolHandle`."""
        self._require_mutable("add_pool")
        if not isinstance(pool, StoragePool):
            raise TypeError(f"add_pool takes a StoragePool, got {type(pool).__name__}")
        if pool.name in self._pools:
            raise ValueError(f"a pool named {pool.name!r} is already registered")
        handle = PoolHandle(pool.name)
        self._pools[pool.name] = pool
        self._pool_handles[pool.name] = handle
        return handle

    def add_switch(self, switch: Switch) -> SwitchHandle:
        """Register an L2 switch; returns its :class:`SwitchHandle`.

        The switch's bindable :class:`~testrange.networks.base.Network`\\ s are
        flattened into :attr:`networks`, so NICs reference
        ``hyp.networks["netA"]`` directly.
        """
        self._require_mutable("add_switch")
        if not isinstance(switch, Switch):
            raise TypeError(f"add_switch takes a Switch, got {type(switch).__name__}")
        if switch.name in self._switches:
            raise ValueError(f"a switch named {switch.name!r} is already registered")
        for net in switch.networks:
            if net.name in self._network_handles:
                raise ValueError(
                    f"a network named {net.name!r} is already registered "
                    f"(on switch {self._network_handles[net.name].switch!r})"
                )
        handle = SwitchHandle(switch.name)
        self._switches[switch.name] = switch
        self._switch_handles[switch.name] = handle
        for net in switch.networks:
            self._network_handles[net.name] = NetworkHandle(net.name, switch=switch.name)
        return handle

    def add_vm(self, recipe: VMRecipe) -> VMHandle:
        """Register a VM recipe; returns its :class:`VMHandle`."""
        self._require_mutable("add_vm")
        if not isinstance(recipe, VMRecipe):
            raise TypeError(f"add_vm takes a VMRecipe, got {type(recipe).__name__}")
        if recipe.name in self._vms:
            raise ValueError(f"a VM named {recipe.name!r} is already registered")
        handle = VMHandle(recipe.name, edges=self)
        self._vms[recipe.name] = recipe
        self._vm_handles[recipe.name] = handle
        return handle

    def add_explicit_edge(self, dependent: str, dependency: str) -> None:
        """Record one explicit ordering edge (the :class:`EdgeSink` hook).

        Called by ``handle.needs(...)``; node-name resolution and cycle
        rejection happen when ``Plan(...)`` assembles the graph.
        """
        self._require_mutable("add an ordering edge")
        self._explicit_edges.append((dependent, dependency))

    @property
    def pools(self) -> Mapping[str, PoolHandle]:
        """Registered pools, by name."""
        return _Registry("pool", self._pool_handles)

    @property
    def switches(self) -> Mapping[str, SwitchHandle]:
        """Registered switches, by name."""
        return _Registry("switch", self._switch_handles)

    @property
    def networks(self) -> Mapping[str, NetworkHandle]:
        """Registered bindable networks (flattened from switches), by name."""
        return _Registry("network", self._network_handles)

    @property
    def vms(self) -> Mapping[str, VMHandle]:
        """Registered VMs, by name."""
        return _Registry("vm", self._vm_handles)

    @property
    def declared_pools(self) -> tuple[StoragePool, ...]:
        """The registered pool declarations, in registration order."""
        return tuple(self._pools.values())

    @property
    def declared_switches(self) -> tuple[Switch, ...]:
        """The registered switch declarations, in registration order."""
        return tuple(self._switches.values())

    @property
    def declared_vms(self) -> tuple[VMRecipe, ...]:
        """The registered VM recipes, in registration order."""
        return tuple(self._vms.values())

    @property
    def explicit_edges(self) -> tuple[tuple[str, str], ...]:
        """Explicit ordering edges recorded by ``handle.needs()``."""
        return tuple(self._explicit_edges)

    @property
    def frozen(self) -> bool:
        """Whether ``Plan(...)`` has sealed this container."""
        return self._frozen

    def freeze(self) -> None:
        """Seal the container against further registration. Idempotent."""
        self._frozen = True

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(pools={len(self._pools)}, "
            f"switches={len(self._switches)}, vms={len(self._vms)}, "
            f"frozen={self._frozen})"
        )


__all__ = ["Hypervisor"]
