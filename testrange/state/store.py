"""StateStore — atomic read/write of state.json + state.pid.

PID-checked: every state-mutation method refuses to operate against a
state file whose owning PID is still alive (clear error). Per PLAN.md
decision 16, this replaces a FileLock — simpler and produces a more
meaningful diagnostic.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import StateError, StateLockedError
from testrange.state.schema import (
    PHASE_DONE,
    PHASE_PREFLIGHT,
    SCHEMA_VERSION,
    Resource,
    State,
)

_log = get_logger(__name__)


def default_state_root() -> Path:
    """``$XDG_STATE_HOME/testrange`` or ``~/.local/state/testrange``."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "testrange"
    return Path.home() / ".local" / "state" / "testrange"


def new_run_id() -> str:
    """Sortable, mostly-unique run id: ``YYYYMMDD-HHMMSS-<6 hex>``."""
    now = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S")
    return f"{now}-{secrets.token_hex(3)}"


def run_dir_for(run_id: str, *, root: Path | None = None) -> Path:
    """Resolve the on-disk directory for a given run id."""
    return (root or default_state_root()) / "runs" / run_id


def _now_utc_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness check.

    POSIX ``kill(pid, 0)``: succeeds (alive) or raises ``ProcessLookupError``
    (dead) or ``PermissionError`` (alive but not ours — assume alive).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover (rare on the dev path)
        return True


class StateStore:
    """File-backed state for one run.

    Layout::

        <run_dir>/
            state.json
            state.pid     # PID of the currently-owning process
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.state_path = run_dir / "state.json"
        self.pid_path = run_dir / "state.pid"

    # ---- lifecycle -----------------------------------------------------

    def initialize(
        self,
        *,
        run_id: str,
        plan_name: str,
        driver_class: str,
        driver_uri: str,
    ) -> State:
        """Create a fresh state.json + state.pid for this run."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            raise StateError(
                f"state already exists at {self.state_path}; "
                "either resume this run or run `testrange cleanup` first"
            )
        state = State(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            plan_name=plan_name,
            driver_class=driver_class,
            driver_uri=driver_uri,
            phase=PHASE_PREFLIGHT,
            created_at=_now_utc_iso(),
            resources=(),
        )
        self._write_state_atomic(state)
        self._write_pid_atomic(os.getpid())
        _log.info("state initialized for run %s at %s", run_id, self.run_dir)
        return state

    def release(self) -> None:
        """Remove state.pid (the run is done; cleanup may safely act)."""
        self.pid_path.unlink(missing_ok=True)

    def remove(self) -> None:
        """Remove the entire run directory (post-cleanup)."""
        if self.state_path.exists():
            self.state_path.unlink(missing_ok=True)
        self.pid_path.unlink(missing_ok=True)
        try:
            self.run_dir.rmdir()
        except OSError:
            pass  # non-empty (foreign files) — leave it

    # ---- access --------------------------------------------------------

    def exists(self) -> bool:
        return self.state_path.exists()

    def read(self) -> State:
        if not self.state_path.exists():
            raise StateError(f"no state at {self.state_path}")
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StateError(f"corrupt state.json at {self.state_path}: {e}") from e
        return State.from_json(data)

    def read_pid(self) -> int | None:
        if not self.pid_path.exists():
            return None
        try:
            return int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def is_owner_alive(self) -> bool:
        """True iff state.pid names a still-running process."""
        pid = self.read_pid()
        if pid is None:
            return False
        return is_pid_alive(pid)

    def require_dead(self) -> None:
        """Raise StateLockedError if the owning PID is still alive."""
        pid = self.read_pid()
        if pid is not None and is_pid_alive(pid):
            raise StateLockedError(
                f"PID {pid} still owns run {self.run_dir.name}; "
                "kill it first or wait for it to exit, then re-run cleanup."
            )

    # ---- mutation ------------------------------------------------------

    def update(self, mutator: Callable[[State], State]) -> State:
        """Read, apply mutator, write atomically. Returns the new state."""
        current = self.read()
        new = mutator(current)
        self._write_state_atomic(new)
        return new

    def record_intent(
        self,
        *,
        kind: str,
        backend_name: str,
        plan_name: str | None = None,
    ) -> Resource:
        """Add a Resource with intent_at set but outcome_at=None.

        Call this BEFORE the backend create-call. Per PLAN.md, this is the
        record-before-create invariant: if the process dies between this
        call and the backend confirming the resource, cleanup still finds
        the deterministic backend_name and tries to destroy it.
        """
        r = Resource(
            kind=kind,
            backend_name=backend_name,
            plan_name=plan_name,
            intent_at=_now_utc_iso(),
        )
        self.update(lambda s: s.with_resource(r))
        return r

    def confirm(self, backend_name: str, **metadata: object) -> None:
        """Set outcome_at on the matching resource and merge metadata."""
        outcome = _now_utc_iso()

        def _f(s: State) -> State:
            for r in s.resources:
                if r.backend_name == backend_name:
                    return s.replace_resource(
                        backend_name,
                        r.with_outcome(outcome, **metadata),
                    )
            raise StateError(f"confirm: no resource named {backend_name!r} in state")

        self.update(_f)

    def forget(self, backend_name: str) -> None:
        """Remove a resource from state.json (post-successful-destroy)."""
        self.update(lambda s: s.remove_resource(backend_name))

    def set_phase(self, phase: str) -> None:
        self.update(lambda s: replace(s, phase=phase))

    def mark_done(self) -> None:
        self.update(lambda s: s.with_phase(PHASE_DONE))

    # ---- atomic write helpers -----------------------------------------

    def _write_state_atomic(self, state: State) -> None:
        text = json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n"
        tmp = self.state_path.with_suffix(".json.partial")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _write_pid_atomic(self, pid: int) -> None:
        tmp = self.pid_path.with_suffix(".pid.partial")
        tmp.write_text(f"{pid}\n", encoding="utf-8")
        os.replace(tmp, self.pid_path)
