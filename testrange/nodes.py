"""The MVP node kinds and the plan -> graph assembly (ADR-0030, DAG-3/DAG-7).

Concrete :class:`~testrange.graph.node.Node` kinds over the pure graph model:
``PoolNode`` / ``NetworkNode`` wrap the existing pool / switch+sidecar value
types; ``VMNode`` wraps a :class:`~testrange.vms.recipe.VMRecipe`. Their
``materialize`` / ``realize`` bodies drive the backend-agnostic driver ABC
through the executor-supplied :class:`~testrange.orchestrator.context.GraphContext`
— the per-resource mechanics live in ``orchestrator/vm_build.py`` /
``vm_run.py`` / ``provision.py``; a node body is the per-kind sequence over
them.

:func:`assemble_graph` is what ``Plan(name, hyp)`` calls to freeze a
:class:`~testrange.hypervisor.Hypervisor` container into the validated
:class:`~testrange.graph.build_graph.BuildGraph`: one node per registered
pool/switch/VM, plus the **implicit infra edges** inferred from the typed
handle references in each VM's spec (VM -> its disks' pools, VM -> its NICs'
switches, a sidecar-carrying switch -> the first declared pool) and the
explicit ordering edges recorded by ``handle.needs()``.

Node names are kind-qualified (``pool:pool1``, ``network:switch1``,
``vm:web``) so a pool and a VM sharing a plan-level name can never collide on
graph identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING

from testrange.exceptions import OrchestratorError
from testrange.graph.build_graph import BuildGraph
from testrange.graph.edge import Edge
from testrange.graph.node import Node, NodeContext
from testrange.orchestrator.build import resolve_build_switch
from testrange.orchestrator.context import GraphContext
from testrange.orchestrator.provision import materialize_sidecar_for, provision_switch
from testrange.orchestrator.vm_build import (
    build_one_vm,
    ensure_build_infra,
    probe_vm,
    resolve_sidecar_sha,
)
from testrange.orchestrator.vm_run import (
    await_guest_ready,
    bind_communicator_for,
    bring_up_vm,
    create_pool_backend,
    wait_communicator_ready,
    wait_sidecar_ready,
    wait_vm_dhcp_leases,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.devices.pool.base import StoragePool
    from testrange.hypervisor import Hypervisor
    from testrange.networks.base import Switch
    from testrange.vms.recipe import VMRecipe


def _graph_ctx(ctx: NodeContext) -> GraphContext:
    """Narrow the structural :class:`NodeContext` to the executor's context.

    The hooks are typed against the empty pure-model protocol (the graph
    package must not know the orchestrator); only the executor invokes them,
    and it always passes a :class:`GraphContext`. Fail loud if some other
    caller hands a node a context it cannot run on.
    """
    if not isinstance(ctx, GraphContext):
        raise OrchestratorError(
            f"node hooks require the executor's GraphContext, got {type(ctx).__name__}"
        )
    return ctx


def _fold_dependency_keys(own_key: str, dependency_keys: Mapping[str, str]) -> str:
    """Fold content-dependency keys into a node's own-inputs key (DAG-5).

    With no content dependencies the node's key IS its own-inputs key — for a
    VM that is the v0 ``config_hash``, byte-identical, so MVP graphs (ordering
    edges only) never bust the existing cache. Cacheable edges fold in
    deterministically (sorted by dependency node name).
    """
    if not dependency_keys:
        return own_key
    h = hashlib.sha256(own_key.encode())
    for dep_name in sorted(dependency_keys):
        h.update(b"\x00")
        h.update(dep_name.encode())
        h.update(b"=")
        h.update(dependency_keys[dep_name].encode())
    return h.hexdigest()[:16]


def _hash_inputs(*parts: str) -> str:
    """16-hex own-inputs hash over a node's declared shape (pure, ordered)."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()[:16]


class PoolNode(Node):
    """A declared storage pool. Materialize is a no-op (nothing is cached for
    infra in the MVP); realize creates the per-run pool on the backend."""

    def __init__(self, pool: StoragePool) -> None:
        self._pool = pool

    @property
    def pool(self) -> StoragePool:
        return self._pool

    @property
    def name(self) -> str:
        return f"pool:{self._pool.name}"

    @property
    def kind(self) -> str:
        return "pool"

    def cache_key(self, ctx: NodeContext, dependency_keys: Mapping[str, str]) -> str:
        del ctx  # a pool's key is pure declaration: name + capacity
        own = _hash_inputs("pool", self._pool.name, str(self._pool.size_gb))
        return _fold_dependency_keys(own, dependency_keys)

    def materialize(self, ctx: NodeContext) -> None:
        del ctx  # nothing to build or cache for a pool

    def realize(self, ctx: NodeContext) -> None:
        rt = _graph_ctx(ctx)
        if rt.resume and self.name in rt.realized_nodes:
            # Reattach (DAG-9): the pool exists from the resumed run; rebuild
            # the in-memory ledger from the deterministic backend name.
            backend = rt.driver.compose_resource_name(rt.run_id, "pool", self._pool.name)
            with rt.ledger_lock:
                rt.pool_backends[self._pool.name] = backend
            return
        create_pool_backend(rt, self._pool)


class NetworkNode(Node):
    """One switch's L2 unit: fabric + bindable networks + optional sidecar.

    A switch and its networks realize together (the sidecar reads the switch's
    own network backends), so they are ONE node; ``hyp.networks["netA"]``
    handles resolve onto the owning switch's node. Realize provisions the
    fabric, materializes the sidecar, and does not return until the sidecar is
    *serving* — so a VM node depending on this network never boots before
    DHCP/DNS/NAT is live (the v0 sidecar barrier, per-node).
    """

    def __init__(self, switch: Switch) -> None:
        self._switch = switch

    @property
    def switch(self) -> Switch:
        return self._switch

    @property
    def name(self) -> str:
        return f"network:{self._switch.name}"

    @property
    def kind(self) -> str:
        return "network"

    def cache_key(self, ctx: NodeContext, dependency_keys: Mapping[str, str]) -> str:
        del ctx  # a switch's key is pure declaration
        sc = self._switch.sidecar
        own = _hash_inputs(
            "network",
            self._switch.name,
            self._switch.cidr,
            self._switch.uplink or "",
            str(self._switch.mgmt),
            ",".join(n.name for n in self._switch.networks),
            "" if sc is None else f"dhcp={sc.dhcp},dns={sc.dns},nat={sc.nat},addr={sc.addr}",
        )
        return _fold_dependency_keys(own, dependency_keys)

    def materialize(self, ctx: NodeContext) -> None:
        del ctx  # nothing to build or cache for a switch

    def realize(self, ctx: NodeContext) -> None:
        rt = _graph_ctx(ctx)
        if rt.resume and self.name in rt.realized_nodes:
            # Reattach (DAG-9): fabric + sidecar exist from the resumed run;
            # rebuild the in-memory ledgers from the deterministic backend
            # names (the uplink segment entry is only consumed at sidecar
            # creation, so it needs no reconstruction), then re-verify the
            # sidecar is still serving before dependents proceed.
            sw = self._switch
            with rt.ledger_lock:
                rt.switch_backends[sw.name] = rt.driver.compose_resource_name(
                    rt.run_id, "switch", sw.name
                )
                for net in sw.networks:
                    rt.network_backends[net.name] = rt.driver.compose_resource_name(
                        rt.run_id, "network", net.name
                    )
                if sw.needs_sidecar:
                    rt.sidecar_backends[sw.name] = rt.driver.compose_resource_name(
                        rt.run_id, "sidecar_vm", sw.name
                    )
            wait_sidecar_ready(rt, sw)
            return
        provision_switch(rt, self._switch)
        materialize_sidecar_for(rt, self._switch)
        wait_sidecar_ready(rt, self._switch)


class VMNode(Node):
    """A declared VM. Materialize warms its cached disk set (building on a
    miss via the shared ephemeral build infra); realize brings it up from the
    cache, binds its communicator, and gates on full readiness.

    ``index`` is the VM's registration position — the deterministic basis for
    its build-switch address slot (the build IP feeds ``config_hash``, so it
    must be plan-stable, never scheduling-dependent).
    """

    def __init__(self, recipe: VMRecipe, index: int) -> None:
        self._recipe = recipe
        self._index = index

    @property
    def recipe(self) -> VMRecipe:
        return self._recipe

    @property
    def index(self) -> int:
        return self._index

    @property
    def name(self) -> str:
        return f"vm:{self._recipe.name}"

    @property
    def kind(self) -> str:
        return "vm"

    def cache_key(self, ctx: NodeContext, dependency_keys: Mapping[str, str]) -> str:
        """The VM's disk-set key: the builder's ``config_hash``, exactly.

        Resolving it probes the cache as a side effect; the probe (resolved
        origin path, MACs, build NIC, native agent, per-role hit/miss) is
        ledgered in ``ctx.vm_probes`` so ``materialize`` and the key see
        identical inputs. With no content dependencies (every MVP graph) the
        returned key is byte-identical to the v0 per-build hash.
        """
        rt = _graph_ctx(ctx)
        probe = rt.vm_probes.get(self.name)
        if probe is None:
            probe = probe_vm(
                rt,
                self._recipe,
                self._index,
                resolve_sidecar_sha(rt),
                resolve_build_switch(rt.plan.hypervisor.build_switch),
            )
            rt.vm_probes[self.name] = probe
        return _fold_dependency_keys(probe.config_hash, dependency_keys)

    def materialize(self, ctx: NodeContext) -> None:
        rt = _graph_ctx(ctx)
        probe = rt.vm_probes.get(self.name)
        if probe is None:
            raise OrchestratorError(
                f"{self.name}: materialize before the executor's key walk "
                "(ctx.vm_probes is unpopulated)"
            )
        if probe.cached_paths is not None:
            with rt.ledger_lock:
                rt.built_disk_paths[self._recipe.name] = dict(probe.cached_paths)
            return
        pool_backend, net_backend = ensure_build_infra(rt)
        build_one_vm(rt, probe, pool_backend, net_backend)

    def realize(self, ctx: NodeContext) -> None:
        rt = _graph_ctx(ctx)
        vm = self._recipe
        if not (rt.resume and self.name in rt.realized_nodes):
            # Reattach (DAG-9) skips creation: the VM is already up from the
            # resumed run. Everything below is an idempotent readiness ensure —
            # the fresh process must still bind its own communicator instance.
            bring_up_vm(rt, vm)
        bind_communicator_for(rt, vm)
        wait_communicator_ready(rt, vm)
        wait_vm_dhcp_leases(rt, vm)
        await_guest_ready(rt, vm)


def assemble_graph(name: str, hyp: Hypervisor) -> BuildGraph:
    """Freeze a Hypervisor container into the validated :class:`BuildGraph`.

    Emits one node per registered pool/switch/VM, the implicit infra edges
    inferred from the spec's typed handle references, and the explicit
    ``.needs()`` edges. Every edge is ``ORDERING`` in the MVP. Construction
    validates the result (duplicate names, dangling references, self-edges,
    cycles) — a handle minted outside this container surfaces here as a
    ``DanglingDependencyError`` (a ``PlanError``).
    """
    nodes: list[Node] = []
    edges: dict[tuple[str, str], Edge] = {}

    def add_edge(dependent: str, dependency: str) -> None:
        edges.setdefault((dependent, dependency), Edge(dependent, dependency))

    first_pool = hyp.declared_pools[0] if hyp.declared_pools else None
    for pool in hyp.declared_pools:
        nodes.append(PoolNode(pool))
    for switch in hyp.declared_switches:
        node = NetworkNode(switch)
        nodes.append(node)
        if switch.needs_sidecar:
            # The sidecar VM's disk lands in the first declared pool (the v0
            # placement, preserved), so a sidecar-carrying switch realizes
            # only after that pool exists.
            if first_pool is None:
                raise OrchestratorError(
                    f"switch {switch.name!r} needs a sidecar but the plan declares no "
                    "pools; the sidecar's disk lands in the first declared pool"
                )
            add_edge(node.name, f"pool:{first_pool.name}")
    for index, vm in enumerate(hyp.declared_vms):
        vm_node = VMNode(vm, index)
        nodes.append(vm_node)
        add_edge(vm_node.name, vm.spec.os_drive.pool.node_name)
        for hd in vm.spec.data_drives:
            add_edge(vm_node.name, hd.pool.node_name)
        for nic in vm.spec.nics:
            add_edge(vm_node.name, nic.network.node_name)
    for dependent, dependency in hyp.explicit_edges:
        add_edge(dependent, dependency)

    return BuildGraph(name, nodes, tuple(edges.values()))


__all__ = [
    "NetworkNode",
    "PoolNode",
    "VMNode",
    "assemble_graph",
]
