"""StateStore — crash-safe state.json + state.pid for one run.

TestRange is single-instance: one ``testrange`` process per user/profile at
a time (ADR-0018). These writes are *not* a concurrency mechanism. They use
``.partial`` + ``os.replace`` so a crash (SIGKILL, power loss) mid-write
leaves state.json fully-old or fully-new, never torn — they do not serialize
two writers, because by contract there is never more than one.

The owning process holds an exclusive advisory lock on ``state.lock``
(``fcntl.flock``) for the run's lifetime; ``state.pid`` is a human-readable
breadcrumb only. The ``cleanup`` recovery tool calls ``require_dead()``, which
probes that lock — a live owner holds it, a dead/crashed owner's lock was
released by the kernel — so cleanup never races a live owner and a recycled PID
can't fool it (ADR-0018, CORE-30: supersedes the old PID-liveness check).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import secrets
from dataclasses import replace
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import StateError, StateLockedError
from testrange.state.schema import (
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
        self.lock_path = run_dir / "state.lock"
        self._lock_fd: int | None = None

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
        # The advisory lock is the ownership guard — held until release/remove,
        # and auto-released by the kernel if this process crashes, so a recycled
        # PID can never masquerade as the owner (ADR-0018). It is acquired before
        # any state file is written, so the `cleanup` recovery tool (which gates
        # on the lock, not on state.json existing) can never tear down a run
        # mid-bring-up. state.pid is written purely as a human-readable breadcrumb.
        self._acquire_lock()
        self._write_pid_atomic(os.getpid())
        self._write_state_atomic(state)
        _log.info("state initialized for run %s at %s", run_id, self.run_dir)
        return state

    def release(self) -> None:
        """Drop ownership (the run is done; cleanup may safely act).

        Releases the advisory lock and removes the breadcrumb pid file.
        """
        self._release_lock()
        self.pid_path.unlink(missing_ok=True)

    def remove(self) -> None:
        """Remove the entire run directory (post-cleanup)."""
        self._release_lock()
        if self.state_path.exists():
            self.state_path.unlink(missing_ok=True)
        self.pid_path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # non-empty (foreign files) — leave it
            self.run_dir.rmdir()

    def _acquire_lock(self) -> None:
        """Take the exclusive advisory lock that marks this process as the run's
        owner, holding it for the run's lifetime.

        Idempotent within an instance. Raises :class:`StateLockedError` if a live
        process already owns the run (holds the lock).
        """
        if self._lock_fd is not None:
            return
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise StateLockedError(
                f"run {self.run_dir.name} is owned by a live process "
                "(state.lock held); kill it or wait for it to exit."
            ) from e
        self._lock_fd = fd

    def _release_lock(self) -> None:
        """Release the advisory lock if this instance holds it."""
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

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
        """Raise StateLockedError if the run's owner is still alive.

        Probes the advisory lock without holding it: a live owner holds
        ``state.lock`` (flock), so a non-blocking acquire fails; a dead/crashed
        owner's lock was released by the kernel, so the acquire succeeds and we
        immediately drop it. This closes the PID-reuse window the old
        liveness check had (ADR-0018, CORE-30).
        """
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise StateLockedError(
                f"run {self.run_dir.name} is owned by a live process "
                "(state.lock held); kill it or wait for it to exit, then re-run cleanup."
            ) from e
        finally:
            os.close(fd)

    def record_intent(
        self,
        *,
        kind: str,
        backend_name: str,
        plan_name: str | None = None,
        **metadata: object,
    ) -> Resource:
        """Add a Resource with intent_at set but outcome_at=None.

        Call this BEFORE the backend create-call. Record-before-create
        invariant: if the process dies between this call and the backend
        confirming the resource, cleanup still finds the deterministic
        backend_name and tries to destroy it.

        ``**metadata`` is stored on the resource at intent time so that
        ``destroy`` can dispatch correctly even if the backend create fails
        before ``confirm`` is reached (e.g., volume kinds need a
        ``pool_backend`` to route through ``delete_volume``).
        """
        r = Resource(
            kind=kind,
            backend_name=backend_name,
            plan_name=plan_name,
            intent_at=_now_utc_iso(),
            metadata=metadata,
        )
        self._write_state_atomic(self.read().with_resource(r))
        return r

    def confirm(self, backend_name: str, **metadata: object) -> None:
        """Set outcome_at on the matching resource and merge metadata."""
        outcome = _now_utc_iso()
        state = self.read()
        for r in state.resources:
            if r.backend_name == backend_name:
                self._write_state_atomic(
                    state.replace_resource(backend_name, r.with_outcome(outcome, **metadata))
                )
                return
        raise StateError(f"confirm: no resource named {backend_name!r} in state")

    def forget(self, backend_name: str) -> None:
        """Remove a resource from state.json (post-successful-destroy)."""
        self._write_state_atomic(self.read().remove_resource(backend_name))

    def set_phase(self, phase: str) -> None:
        self._write_state_atomic(replace(self.read(), phase=phase))

    def _write_state_atomic(self, state: State) -> None:
        text = json.dumps(state.to_json(), indent=2, sort_keys=True) + "\n"
        tmp = self.state_path.with_suffix(".json.partial")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.state_path)

    def _write_pid_atomic(self, pid: int) -> None:
        tmp = self.pid_path.with_suffix(".pid.partial")
        tmp.write_text(f"{pid}\n", encoding="utf-8")
        tmp.replace(self.pid_path)
