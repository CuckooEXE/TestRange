"""Per-run state shared across the orchestrator's phases.

The :class:`RunContext` bundles the immutable collaborators a run needs
(driver, state store, cache, identifiers, addressing) together with the
mutable resource ledger written during bring-up and read back at teardown.
Phase functions take a ``RunContext`` explicitly so each one's reads and
writes are visible at its call site rather than hidden behind ``self``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from testrange.cache.manager import CacheManager
from testrange.drivers.base import HypervisorDriver
from testrange.networks.base import NetworkAddressing
from testrange.orchestrator.backend import ResolvedBackend
from testrange.plan import Plan
from testrange.state.store import StateStore


@dataclass(frozen=True)
class RunContext:
    """State for one orchestrated run.

    Immutable collaborators are set at construction; the ledger fields are
    mutated in place as resources are created (and emptied as they are torn
    down). The dataclass is frozen against field *rebinding* — the ledger
    containers are still mutated through their own methods.
    """

    plan: Plan
    # The resolved backend binding (CORE-10): driver + declared build switch +
    # teardown URI. ``driver`` is exposed as a property so the phases keep
    # reading ``ctx.driver`` while the binding stays the single source of truth.
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
    # How long the run phase waits for each sidecar's native guest agent to
    # answer + apply its config before failing loud (ADR-0010 §8).
    sidecar_ready_timeout_s: float = 120.0
    # How long the run phase waits for each user VM's bound communicator to
    # answer (native agent or SSH) after boot, before the first readiness
    # probe — the agent starts a few seconds after power-on and must not be
    # raced. Same budget shape as the sidecar wait.
    agent_ready_timeout_s: float = 120.0
    # Worker cap for the I/O phases' bounded thread pool (ADR-0020). ``None``
    # uses the default cap (``parallel_map``'s :data:`DEFAULT_MAX_WORKERS`);
    # ``1`` forces the phases serial (the ``--jobs`` CLI knob).
    jobs: int | None = None

    # Resource ledger — written during bring-up, read at teardown.
    pool_backends: dict[str, str] = field(default_factory=dict)  # plan_name -> backend
    network_backends: dict[str, str] = field(default_factory=dict)  # plan_name -> backend
    switch_backends: dict[str, str] = field(default_factory=dict)  # switch -> switch backend
    sidecar_backends: dict[str, str] = field(default_factory=dict)  # switch -> sidecar VM backend
    # vm name -> {role -> cached disk path}, e.g. {"web": {"os": ..., "data0": ...}}.
    # Populated by the build phase (capture or cache-hit); read by the run phase.
    built_disk_paths: dict[str, dict[str, Path]] = field(default_factory=dict)

    # Guards the in-memory ledger dicts above when the I/O phases mutate them
    # from multiple worker threads (ADR-0020). Held only for the quick dict
    # update; the slow backend call around it runs unlocked on the shared,
    # thread-safe driver connection. The cross-process state.json is serialized
    # separately inside StateStore.
    ledger_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def driver(self) -> HypervisorDriver:
        return self.resolved.driver


__all__ = ["RunContext"]
