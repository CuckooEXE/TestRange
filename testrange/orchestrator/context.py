"""Per-run state shared across the executor's graph walks.

The :class:`GraphContext` is the concrete object the DAG executor hands every
node hook — the executor-side widening of the empty
:class:`~testrange.graph.node.NodeContext` protocol (ADR-0030, DAG-6). It
bundles the immutable collaborators a run needs (driver, state store, cache,
identifiers, addressing) with the mutable, lock-guarded ledgers written during
bring-up and read back at teardown. Node hooks and the helper functions they
call take it explicitly, so each one's reads and writes are visible at the
call site rather than hidden behind ``self``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from testrange.cache.manager import CacheManager
from testrange.drivers.base import HypervisorDriver
from testrange.networks.base import NetworkAddressing, Switch
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.dashboard_state import DashboardState
from testrange.state.store import StateStore

if TYPE_CHECKING:  # pragma: no cover
    from testrange.orchestrator.vm_build import VMBuildProbe
    from testrange.plan import Plan


@dataclass
class BuildInfraState:
    """The shared ephemeral build infra (pool + switch + sidecar), once per run.

    Build VMs all ride one build pool/switch (ADR-0010 §2/§9). VM nodes
    materialize concurrently, so the infra is created lazily by the *first*
    cache-missing VM (``ensure_build_infra``) under :attr:`lock`, and torn down
    by the executor after the materialize walk. Holds state only; the
    create/teardown logic lives in ``orchestrator/vm_build.py``.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    active: bool = False
    pool_backend: str | None = None
    net_backend: str | None = None
    build_switch: Switch | None = None


@dataclass(frozen=True)
class GraphContext:
    """State for one orchestrated run, as seen by every node hook.

    Immutable collaborators are set at construction; the ledger fields are
    mutated in place as resources are created (and emptied as they are torn
    down). The dataclass is frozen against field *rebinding* — the ledger
    containers are still mutated through their own methods, briefly, under
    :attr:`ledger_lock` (ADR-0023).
    """

    plan: Plan
    # The resolved backend binding (CORE-10): driver + uplink maps + teardown
    # URI. ``driver`` is exposed as a property so hooks read ``ctx.driver``
    # while the binding stays the single source of truth.
    resolved: ResolvedBackend
    store: StateStore
    cache: CacheManager
    run_id: str
    plan_name: str
    build_timeout_s: float
    lease_timeout_s: float
    # Builder-facing addressing map. The orchestrator brokers per the
    # stovepipe rule: builders never see a hypervisor type, they get the
    # one piece of info they need — per-network CIDR/prefix/gateway/dhcp.
    addressing: Mapping[str, NetworkAddressing]
    # How long a network node's realize waits for its sidecar's native guest
    # agent to answer + apply its config before failing loud (ADR-0010 §8).
    sidecar_ready_timeout_s: float = 120.0
    # How long a VM node's realize waits for the bound communicator to answer
    # (native agent or SSH) after boot, before the first readiness probe — the
    # agent starts a few seconds after power-on and must not be raced.
    agent_ready_timeout_s: float = 120.0
    # Worker cap for the walks' bounded thread pool (ADR-0023). ``None`` uses
    # the default cap; ``1`` forces serial (the ``--jobs`` CLI knob).
    jobs: int | None = None
    # ``run --resume <run_id>`` (DAG-9): skip nodes whose completion is already
    # stamped in the reopened state ledger.
    resume: bool = False
    # Whether the cache-key walk's probes materialize what they resolve.
    # True for the executor (a build needs the bytes; the cold-cache fetch is
    # the deliberate ADR-0010 §2 penalty). False for the read-only inspection
    # path (``graph --cache``), which must answer hit/miss from metadata alone
    # and never pull a multi-GB artifact to print one line.
    probe_fetch: bool = True

    # Resource ledger — written during bring-up, read at teardown.
    pool_backends: dict[str, str] = field(default_factory=dict)  # plan_name -> backend
    network_backends: dict[str, str] = field(default_factory=dict)  # plan_name -> backend
    switch_backends: dict[str, str] = field(default_factory=dict)  # switch -> switch backend
    sidecar_backends: dict[str, str] = field(default_factory=dict)  # switch -> sidecar VM backend
    # vm name -> {role -> cached disk path}, e.g. {"web": {"os": ..., "data0": ...}}.
    # Populated by VM-node materialize (capture or cache-hit); read by realize.
    built_disk_paths: dict[str, dict[str, Path]] = field(default_factory=dict)

    # Graph-walk ledger (DAG-5/DAG-6): every node's content-addressed key and
    # each VM node's resolved build probe, both filled by the executor's serial
    # key walk before any wave dispatch.
    node_keys: dict[str, str] = field(default_factory=dict)
    vm_probes: dict[str, VMBuildProbe] = field(default_factory=dict)
    # Node names whose materialize/realize completed (in-memory mirror of the
    # state ledger's NodeRecord stamps; pre-seeded from disk on --resume).
    materialized_nodes: set[str] = field(default_factory=set)
    realized_nodes: set[str] = field(default_factory=set)

    # Lazily-created shared build infra (see BuildInfraState).
    build_infra: BuildInfraState = field(default_factory=BuildInfraState, repr=False)

    # Guards the in-memory ledger dicts/sets above when wave workers mutate
    # them concurrently (ADR-0023). Held only for the quick container update;
    # the slow backend call around it runs unlocked on the shared, thread-safe
    # driver connection. The cross-process state.json is serialized separately
    # inside StateStore.
    ledger_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    # Live-dashboard state (ADR-0029): node hooks report VM lifecycle stages and
    # test outcomes into this; the CLI's renderer reads its snapshots. It carries
    # its own lock (distinct from ledger_lock), so dashboard writes never contend
    # with resource bookkeeping. A run with no dashboard wired still gets a fresh
    # one here — the set_vm_stage calls are then cheap no-ops nobody renders.
    dashboard: DashboardState = field(default_factory=DashboardState, compare=False, repr=False)

    @property
    def driver(self) -> HypervisorDriver:
        return self.resolved.driver


__all__ = ["BuildInfraState", "GraphContext"]
