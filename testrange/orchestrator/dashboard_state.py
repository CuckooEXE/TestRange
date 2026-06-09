"""Thread-safe state behind the live ``run``/``build`` dashboard (ADR-0029).

This module is **rich-free on purpose**: the orchestrator's phase and driver
code reports lifecycle state into a :class:`DashboardState` without importing the
renderer (``testrange._dashboard``), so the UI dependency never leaks down into
the deep code (the stovepipe rule). The renderer reads only an immutable
:class:`DashboardSnapshot`.

The I/O phases mutate this from several worker threads (``parallel_map``,
ADR-0023) while rich's ``Live`` refresh thread reads it, so every access takes a
**dedicated** lock — not the ledger lock — held only for the in-memory
dict/deque op. Records are frozen and replaced on write, so a
:meth:`DashboardState.snapshot` hands the renderer a consistent, immutable view
that no later mutation can change.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Literal

# Default ring sizes: the panes only show their last few lines, so a few
# hundred entries is ample scrollback without unbounded growth.
_LOG_RING = 200
_SERIAL_RING = 500


class VMStage(StrEnum):
    """A VM's coarse lifecycle stage, in the order a run walks it.

    Transitions are *not* asserted monotonic — a VM goes through these in the
    build phase and again in the run phase — so a setter just overwrites.
    """

    PENDING = "pending"
    PROVISIONING = "provisioning"
    BUILDING = "building"
    BOOTING = "booting"
    BINDING = "binding"
    READY = "ready"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """True once the VM has reached a final state (won't advance further)."""
        return self in (VMStage.READY, VMStage.FAILED)


TestStatus = Literal["running", "passed", "failed"]


@dataclass(frozen=True)
class VMRecord:
    """Internal per-VM record (frozen; replaced on write under the lock)."""

    name: str
    stage: VMStage = VMStage.PENDING
    started_at: float | None = None  # monotonic, set on first non-PENDING stage
    detail: str | None = None  # last error / note, surfaced in the VM pane


@dataclass(frozen=True)
class VMView:
    """Immutable per-VM view handed to the renderer (``elapsed`` precomputed)."""

    name: str
    stage: VMStage
    elapsed: float | None  # seconds since first activity, None while PENDING
    detail: str | None


@dataclass(frozen=True)
class TestRecord:
    """Immutable per-test record (replaced wholesale on start/finish)."""

    name: str
    status: TestStatus = "running"
    duration: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class DashboardSnapshot:
    """A consistent point-in-time view of the dashboard, safe to render lock-free."""

    vms: tuple[VMView, ...]
    tests: tuple[TestRecord, ...]
    log_lines: tuple[str, ...]
    serial_lines: tuple[tuple[str, str], ...]  # (vm_name, line)


class DashboardState:
    """Mutable dashboard state shared across worker threads and the renderer.

    Every method takes :attr:`_lock` for the duration of the in-memory op only;
    nothing slow runs under it. Insertion order of ``vms``/``tests`` is preserved
    (plan order for VMs, execution order for tests).
    """

    def __init__(self, *, log_maxlen: int = _LOG_RING, serial_maxlen: int = _SERIAL_RING) -> None:
        self._lock = RLock()
        self._vms: dict[str, VMRecord] = {}
        self._tests: dict[str, TestRecord] = {}
        self._log_ring: deque[str] = deque(maxlen=log_maxlen)
        self._serial_ring: deque[tuple[str, str]] = deque(maxlen=serial_maxlen)

    def seed_vms(self, names: Iterable[str]) -> None:
        """Register VMs as PENDING in plan order (idempotent; keeps existing state)."""
        with self._lock:
            for name in names:
                self._vms.setdefault(name, VMRecord(name=name))

    def set_vm_stage(self, name: str, stage: VMStage, *, detail: str | None = None) -> None:
        """Advance ``name`` to ``stage`` (registering it if unseen); stamp first activity."""
        now = monotonic()
        with self._lock:
            rec = self._vms.get(name, VMRecord(name=name))
            started = rec.started_at
            if started is None and stage is not VMStage.PENDING:
                started = now
            self._vms[name] = replace(
                rec,
                stage=stage,
                started_at=started,
                detail=detail if detail is not None else rec.detail,
            )

    def abort_unfinished(self, *, detail: str = "aborted (run interrupted)") -> None:
        """Sweep every still-non-terminal VM to FAILED.

        Called when a run unwinds on an error: ``parallel_map`` is fail-fast, so
        the VM whose worker raised is already marked FAILED by its own handler,
        but its siblings can be left mid-stage. This drives the final frame to a
        truthful terminal state instead of a row frozen at ``booting``.
        """
        with self._lock:
            for name, rec in self._vms.items():
                if not rec.stage.is_terminal:
                    self._vms[name] = replace(
                        rec, stage=VMStage.FAILED, detail=rec.detail or detail
                    )

    def start_test(self, name: str) -> None:
        """Mark ``name`` as currently running."""
        with self._lock:
            self._tests[name] = TestRecord(name=name, status="running")

    def finish_test(
        self, name: str, *, passed: bool, duration: float, error: str | None = None
    ) -> None:
        """Record ``name``'s terminal outcome (primitives, so this module stays runner-free)."""
        with self._lock:
            self._tests[name] = TestRecord(
                name=name,
                status="passed" if passed else "failed",
                duration=duration,
                error=error,
            )

    def append_log(self, line: str) -> None:
        """Append a progress log line to the rolling log pane buffer."""
        with self._lock:
            self._log_ring.append(line)

    def append_serial(self, vm: str, line: str) -> None:
        """Append a (already-scrubbed) serial-console line tagged with its VM."""
        with self._lock:
            self._serial_ring.append((vm, line))

    def snapshot(self) -> DashboardSnapshot:
        """Return a consistent immutable view; ``elapsed`` is computed under the lock."""
        now = monotonic()
        with self._lock:
            vms = tuple(
                VMView(
                    name=rec.name,
                    stage=rec.stage,
                    elapsed=None if rec.started_at is None else now - rec.started_at,
                    detail=rec.detail,
                )
                for rec in self._vms.values()
            )
            return DashboardSnapshot(
                vms=vms,
                tests=tuple(self._tests.values()),
                log_lines=tuple(self._log_ring),
                serial_lines=tuple(self._serial_ring),
            )


__all__ = [
    "DashboardSnapshot",
    "DashboardState",
    "TestRecord",
    "TestStatus",
    "VMRecord",
    "VMStage",
    "VMView",
]
