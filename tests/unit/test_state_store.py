"""Tests for StateStore (single-instance crash-safe writes, PID gating).

TestRange is single-instance (ADR-0018); these tests pin the contract that
the atomic-rename writes buy crash safety for the single owner — never a
torn canonical file — and that the PID guard refuses the ``cleanup`` recovery
tool against a still-live owner. They are NOT concurrent-writer tests, because
two writers against one run is unsupported by construction.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from testrange.exceptions import StateError, StateLockedError
from testrange.state import PHASE_DONE
from testrange.state.schema import PHASE_PREFLIGHT
from testrange.state.store import (
    StateStore,
    default_state_root,
    is_pid_alive,
    new_run_id,
    run_dir_for,
)


class TestDefaultRoot:
    def test_xdg_state_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_state_root() == tmp_path / "testrange"

    def test_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert str(default_state_root()).endswith("/.local/state/testrange")


class TestRunId:
    def test_format(self) -> None:
        rid = new_run_id()
        # YYYYMMDD-HHMMSS-<6 hex>
        assert len(rid) == 22
        assert rid[8] == "-"
        assert rid[15] == "-"


class TestPidLiveness:
    def test_current_process_alive(self) -> None:
        assert is_pid_alive(os.getpid())

    def test_unknown_pid_dead(self) -> None:
        # 0xFFFFFF is well beyond normal PID range on Linux.
        assert is_pid_alive(0x7FFFFFFF) is False


class TestStateStore:
    def _store(self, tmp_path: Path) -> StateStore:
        return StateStore(tmp_path / "runs" / "r1")

    def test_initialize_creates_files(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        s = store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        assert s.run_id == "r1"
        assert s.phase == PHASE_PREFLIGHT
        assert store.state_path.exists()
        assert store.pid_path.exists()
        assert int(store.pid_path.read_text().strip()) == os.getpid()

    def test_initialize_twice_errors(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        with pytest.raises(StateError):
            store.initialize(
                run_id="r1",
                plan_name="hello",
                driver_class="MockDriver",
                driver_uri="qemu:///session",
            )

    def test_record_intent_and_confirm(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        store.record_intent(kind="network", backend_name="tr_a_b", plan_name="netA")
        s = store.read()
        assert s.resources[0].outcome_at is None
        store.confirm("tr_a_b", bridge="virbr-1")
        s2 = store.read()
        assert s2.resources[0].outcome_at is not None
        assert s2.resources[0].metadata["bridge"] == "virbr-1"

    def test_confirm_unknown_resource(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        with pytest.raises(StateError):
            store.confirm("nope")

    def test_forget(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        store.record_intent(kind="pool", backend_name="bn", plan_name="pool1")
        store.forget("bn")
        assert store.read().resources == ()

    def test_initialize_lays_down_well_formed_no_partials(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///system",
        )
        # No torn-write residue, and the written state.json is well-formed.
        assert list(store.run_dir.glob("*.partial")) == []
        data = json.loads(store.state_path.read_text())
        assert data["schema_version"] == 1
        assert data["run_id"] == "r1"

    def test_require_dead_when_alive_raises(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        with pytest.raises(StateLockedError):
            store.require_dead()

    def test_require_dead_after_release_passes(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        # Owner exits: drops the advisory lock.
        store.release()
        store.require_dead()  # no raise

    def test_require_dead_ignores_recycled_pid(self, tmp_path: Path) -> None:
        # PID-reuse regression (CORE-30): ownership is the flock, not the pid
        # file. A live PID left in state.pid by a since-exited owner (whose PID
        # got recycled) must NOT make require_dead think the run is still owned.
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        store.release()  # owner gone (lock dropped) ...
        store.pid_path.write_text(f"{os.getpid()}\n")  # ... but a live PID lingers
        store.require_dead()  # no raise — the lock, not the PID, is authoritative

    def test_torn_write_leaves_canonical_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Single-instance crash safety: if the atomic rename fails mid-write
        # (simulated SIGKILL / power loss), the canonical state.json is left
        # fully intact — never a torn/empty file.
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        original = store.state_path.read_text()

        def _boom(src: object, dst: object) -> None:
            raise OSError("simulated crash before rename completes")

        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            store.set_phase(PHASE_DONE)
        # Canonical path untouched: old content still fully readable.
        assert store.state_path.read_text() == original
        assert store.read().phase == PHASE_PREFLIGHT

    def test_set_phase_persists(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///system",
        )
        store.set_phase(PHASE_DONE)
        assert store.read().phase == PHASE_DONE

    def test_read_rejects_unknown_schema_version(self, tmp_path: Path) -> None:
        # ADR-0003: an unrecognized schema_version must fail loud, not silently
        # degrade — its field assumptions can't be trusted for cleanup.
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        data = json.loads(store.state_path.read_text())
        data["schema_version"] = 999
        store.state_path.write_text(json.dumps(data))
        with pytest.raises(StateError, match="schema_version"):
            store.read()

    def test_remove(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        store.remove()
        assert not store.state_path.exists()
        assert not store.pid_path.exists()
        assert not store.lock_path.exists()


class TestRunDirFor:
    def test_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        d = run_dir_for("r-abc")
        assert d == tmp_path / "testrange" / "runs" / "r-abc"


class TestConcurrentRMW:
    """In-process concurrency (the I/O phases run on a thread pool).

    Unlike the cross-process case (still unsupported, ADR-0018), one owning
    process may now drive its bring-up/build on a bounded thread pool. The
    read-modify-write pairs must be atomic against each other or concurrent
    ``record_intent`` calls would clobber one another's additions.
    """

    def _store(self, tmp_path: Path) -> StateStore:
        store = StateStore(tmp_path / "runs" / "r1")
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        return store

    def test_concurrent_record_intent_loses_no_writes(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        n = 64
        barrier = threading.Barrier(n)

        def record(i: int) -> None:
            barrier.wait()  # maximize contention on the RMW pair
            store.record_intent(kind="vm", backend_name=f"vm-{i:03d}", plan_name=f"p{i}")

        threads = [threading.Thread(target=record, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        names = {r.backend_name for r in store.read().resources}
        assert names == {f"vm-{i:03d}" for i in range(n)}

    def test_concurrent_confirm_and_forget(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        n = 32
        for i in range(n):
            store.record_intent(kind="vm", backend_name=f"vm-{i:03d}", plan_name=f"p{i}")

        # Confirm the even ids, forget the odd ones, all concurrently.
        def mutate(i: int) -> None:
            if i % 2 == 0:
                store.confirm(f"vm-{i:03d}")
            else:
                store.forget(f"vm-{i:03d}")

        threads = [threading.Thread(target=mutate, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        resources = {r.backend_name: r for r in store.read().resources}
        assert set(resources) == {f"vm-{i:03d}" for i in range(0, n, 2)}
        assert all(r.outcome_at is not None for r in resources.values())
