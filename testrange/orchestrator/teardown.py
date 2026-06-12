"""LIFO teardown driven by the state store as the source of truth."""

from __future__ import annotations

from testrange._log import get_logger
from testrange.orchestrator.context import GraphContext
from testrange.state.schema import PHASE_CLEANUP, PHASE_DONE

_log = get_logger(__name__)


def teardown(ctx: GraphContext) -> None:
    """LIFO teardown using state.json as the source of truth."""
    # Marking the phase is best-effort bookkeeping; if it fails (disk full,
    # perms) we must STILL attempt the destroys below, or in-flight resources
    # leak. Log and press on.
    try:
        ctx.store.set_phase(PHASE_CLEANUP)
    except Exception as e:
        _log.warning("could not set cleanup phase (continuing teardown): %s", e)

    # The store is the source of truth for what to destroy — if we can't read
    # it, there is no resource list to act on, so bailing here is correct (the
    # destroys, not just bookkeeping, depend on this read).
    try:
        state = ctx.store.read()
    except Exception as e:
        _log.warning("could not read state for teardown: %s", e)
        return

    resources = list(reversed(state.resources))
    total = len(resources)
    if total == 0:
        _log.info("teardown: nothing to do (state has no resources)")
    else:
        _log.info("teardown: %d resource(s) to destroy (LIFO)", total)

    ok = 0
    failed = 0
    for idx, r in enumerate(resources, start=1):
        _log.info("teardown [%d/%d] destroy %s %s", idx, total, r.kind, r.backend_name)
        try:
            ctx.driver.destroy(r.kind, r.backend_name, **dict(r.metadata))
            ctx.store.forget(r.backend_name)
            ok += 1
        except Exception as e:
            failed += 1
            _log.warning(
                "teardown [%d/%d] %s %s failed: %s",
                idx,
                total,
                r.kind,
                r.backend_name,
                e,
            )
    if total > 0:
        _log.info("teardown summary: %d ok, %d failed", ok, failed)

    try:
        remaining = ctx.store.read().resources
    except Exception:
        remaining = ()
    if not remaining:
        # Final bookkeeping is best-effort. teardown() runs from __exit__, so a
        # failure here (disk full, perms) must not raise out and replace the
        # original bring-up exception that triggered the teardown. The backend
        # resources are already destroyed; a leftover state.json is harmless and
        # reclaimable by `testrange cleanup`.
        try:
            ctx.store.set_phase(PHASE_DONE)
            ctx.store.release()
            ctx.store.remove()
        except Exception as e:
            _log.warning(
                "teardown: final state bookkeeping failed (run id=%s): %s",
                ctx.run_id,
                e,
            )
    else:
        _log.warning(
            "teardown: %d resource(s) still recorded in state; run id=%s",
            len(remaining),
            ctx.run_id,
        )


__all__ = ["teardown"]
