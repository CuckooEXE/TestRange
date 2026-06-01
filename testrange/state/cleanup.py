"""State-file-driven cleanup walker.

Cleanup replays ``state.json`` rather than re-deriving resources from the
Plan, because the whole point is to recover runs the orchestrator could
*not* tear down itself — a ``kill -9``, a crash, a power loss. The state
file is the durable ledger of what was actually created (record-before-
create), so reversing it is the only source of truth that survives the
owning process dying. PID-checking guards against the live owner: if the
process that created a run is still running, its own ``__exit__`` owns
teardown and we must not race it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from testrange._log import get_logger
from testrange.drivers import driver_for_name
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import StateError, StateLockedError
from testrange.state.schema import PHASE_CLEANUP, PHASE_DONE
from testrange.state.store import StateStore, default_state_root

_log = get_logger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of cleaning one run — kept granular so the CLI can report
    partial cleanups honestly instead of a single pass/fail.

    The three resource buckets are mutually exclusive per resource and
    deliberately separate: a resource that erased cleanly is very different
    from one we *chose* not to touch (``--dry-run``) or one whose destroy
    raised. Errors carry their message so the operator can act on the real
    cause (still-attached volume, perms) without re-running to find it.
    """

    run_id: str
    destroyed: tuple[str, ...]
    skipped: tuple[str, ...]  # dry-run: would-destroy, not actually destroyed
    errors: tuple[tuple[str, str], ...]  # (backend_name, message)


def find_run_dirs(root: Path | None = None) -> list[Path]:
    """List every existing run directory under the state root."""
    base = (root or default_state_root()) / "runs"
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _instantiate_driver(state_driver_class: str, state_driver_uri: str) -> HypervisorDriver:
    """Re-instantiate the driver named in state.json via the driver registry."""
    return driver_for_name(state_driver_class, state_driver_uri)


def cleanup_run(
    run_id: str,
    *,
    root: Path | None = None,
    dry_run: bool = False,
) -> CleanupResult:
    """Tear down a single run by replaying its state.json in reverse.

    PID-checked: refuses to act if the owning process is still alive.
    """
    run_dir = (root or default_state_root()) / "runs" / run_id
    store = StateStore(run_dir)
    if not store.exists():
        raise StateError(f"no state.json under {run_dir}")
    store.require_dead()

    state = store.read()
    destroyed: list[str] = []
    skipped: list[str] = []
    errors: list[tuple[str, str]] = []

    if dry_run:
        for r in reversed(state.resources):
            _log.info("would destroy %s %s", r.kind, r.backend_name)
            skipped.append(r.backend_name)
        return CleanupResult(
            run_id=run_id,
            destroyed=(),
            skipped=tuple(skipped),
            errors=(),
        )

    driver = _instantiate_driver(state.driver_class, state.driver_uri)
    driver.connect()
    try:
        store.set_phase(PHASE_CLEANUP)
        for r in reversed(state.resources):
            try:
                driver.destroy(r.kind, r.backend_name, **dict(r.metadata))
                store.forget(r.backend_name)
                destroyed.append(r.backend_name)
            except Exception as e:
                _log.warning("destroy %s/%s failed: %s", r.kind, r.backend_name, e)
                errors.append((r.backend_name, str(e)))
    finally:
        driver.disconnect()

    # If we cleaned everything, mark done + remove the dir
    final_state = store.read()
    if not final_state.resources and not errors:
        store.set_phase(PHASE_DONE)
        store.remove()
    elif not final_state.resources:
        store.set_phase(PHASE_DONE)
    return CleanupResult(
        run_id=run_id,
        destroyed=tuple(destroyed),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def cleanup_all(
    *,
    root: Path | None = None,
    dry_run: bool = False,
) -> Iterator[CleanupResult]:
    """Cleanup every run dir under the state root.

    Yields one CleanupResult per run, including failed-PID-check runs
    (recorded as a single "locked" entry).
    """
    for d in find_run_dirs(root):
        run_id = d.name
        try:
            yield cleanup_run(run_id, root=root, dry_run=dry_run)
        except StateLockedError as e:
            _log.warning("skipping locked run %s: %s", run_id, e)
            yield CleanupResult(
                run_id=run_id,
                destroyed=(),
                skipped=(),
                errors=(("(locked)", str(e)),),
            )
        except StateError as e:
            _log.warning("skipping bad run %s: %s", run_id, e)
            yield CleanupResult(
                run_id=run_id,
                destroyed=(),
                skipped=(),
                errors=(("(state)", str(e)),),
            )
        except Exception as e:
            # Cleanup is the recovery path and must attempt every state file
            # independently: a single run whose backend is gone (connect() raises
            # DriverError) or otherwise fails to instantiate must NOT abort the
            # whole sweep — the CLI consumes this generator with list(), so a
            # propagating error would also discard every result already yielded.
            # Record it and move on; the ledger stays on disk for a later retry
            # once the backend is reachable. Mirrors the per-resource broad catch
            # in cleanup_run.
            _log.warning("skipping run %s: cleanup failed: %s", run_id, e)
            yield CleanupResult(
                run_id=run_id,
                destroyed=(),
                skipped=(),
                errors=(("(driver)", str(e)),),
            )


def format_cleanup_results(results: Iterable[CleanupResult]) -> str:
    """Render a CLI-friendly summary."""
    lines = []
    for r in results:
        lines.append(f"run {r.run_id}:")
        if r.destroyed:
            lines.append(f"  destroyed: {', '.join(r.destroyed)}")
        if r.skipped:
            lines.append(f"  would destroy: {', '.join(r.skipped)}")
        if r.errors:
            for name, msg in r.errors:
                lines.append(f"  error on {name}: {msg}")
        if not (r.destroyed or r.skipped or r.errors):
            lines.append("  (nothing to do)")
    return "\n".join(lines) if lines else "(no runs)"
