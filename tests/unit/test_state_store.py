"""Tests for StateStore (atomic writes, PID gating)."""

from __future__ import annotations

import json
import os
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

    def test_require_dead_when_pid_gone(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.initialize(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
        )
        # Simulate the owner exiting
        store.pid_path.write_text("0\n")
        store.require_dead()  # no raise

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


class TestRunDirFor:
    def test_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        d = run_dir_for("r-abc")
        assert d == tmp_path / "testrange" / "runs" / "r-abc"
