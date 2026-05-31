"""State layer — durable record of each run's allocated backend resources."""

from __future__ import annotations

from testrange.state.cleanup import cleanup_run, find_run_dirs
from testrange.state.schema import (
    PHASE_BUILD,
    PHASE_CLEANUP,
    PHASE_DONE,
    PHASE_LEAKED,
    PHASE_PREFLIGHT,
    PHASE_RUN,
    Resource,
    State,
)
from testrange.state.store import StateStore, default_state_root, new_run_id, run_dir_for

__all__ = [
    "PHASE_BUILD",
    "PHASE_CLEANUP",
    "PHASE_DONE",
    "PHASE_LEAKED",
    "PHASE_PREFLIGHT",
    "PHASE_RUN",
    "Resource",
    "State",
    "StateStore",
    "cleanup_run",
    "default_state_root",
    "find_run_dirs",
    "new_run_id",
    "run_dir_for",
]
