"""State-file-driven cleanup walker."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import DriverError, StateError, StateLockedError
from testrange.state.schema import PHASE_CLEANUP, PHASE_DONE
from testrange.state.store import StateStore, default_state_root

_log = get_logger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    run_id: str
    destroyed: tuple[str, ...]
    skipped: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]  # (backend_name, message)


def find_run_dirs(root: Path | None = None) -> list[Path]:
    """List every existing run directory under the state root."""
    base = (root or default_state_root()) / "runs"
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def _instantiate_driver(state_driver_class: str, state_driver_uri: str) -> object:
    """Re-instantiate the driver named in state.json."""
    if state_driver_class == "LibvirtDriver":
        from testrange.drivers.libvirt import LibvirtDriver
        return LibvirtDriver(uri=state_driver_uri)
    raise DriverError(f"unknown driver class in state: {state_driver_class!r}")


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
    # connect / disconnect are on HypervisorDriver
    driver.connect()  # type: ignore[attr-defined]
    try:
        store.set_phase(PHASE_CLEANUP)
        for r in reversed(state.resources):
            try:
                driver.destroy(  # type: ignore[attr-defined]
                    r.kind, r.backend_name, **dict(r.metadata)
                )
                store.forget(r.backend_name)
                destroyed.append(r.backend_name)
            except Exception as e:
                _log.warning("destroy %s/%s failed: %s", r.kind, r.backend_name, e)
                errors.append((r.backend_name, str(e)))
    finally:
        driver.disconnect()  # type: ignore[attr-defined]

    # If we cleaned everything, mark done + remove the dir
    final_state = store.read()
    if not final_state.resources and not errors:
        store.mark_done()
        store.remove()
    else:
        store.set_phase(PHASE_DONE if not final_state.resources else PHASE_CLEANUP)
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
