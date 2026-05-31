"""Tests for state.cleanup — reverse-walking state.json + flock ownership gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.exceptions import StateError, StateLockedError
from testrange.state.cleanup import cleanup_run, find_run_dirs
from testrange.state.schema import PHASE_RUN
from testrange.state.store import StateStore


class _FakeDriver:
    """Stand-in for HypervisorDriver — records destroy() calls."""

    DRIVER_NAME = "FakeDriver"

    def __init__(self, *, uri: str) -> None:
        self.uri = uri
        self.connected = False
        self.destroyed: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def destroy(self, kind: str, backend_name: str) -> None:
        if backend_name in self.fail_on:
            raise RuntimeError(f"simulated failure on {backend_name}")
        self.destroyed.append((kind, backend_name))


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> _FakeDriver:
    driver = _FakeDriver(uri="fake:///x")

    def _instantiate(cls: str, uri: str) -> _FakeDriver:
        assert cls == "FakeDriver"
        driver.uri = uri
        return driver

    monkeypatch.setattr(
        "testrange.state.cleanup._instantiate_driver",
        _instantiate,
    )
    return driver


@pytest.fixture
def populated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, StateStore]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    run_id = "r-1"
    store = StateStore(tmp_path / "testrange" / "runs" / run_id)
    store.initialize(
        run_id=run_id,
        plan_name="hello",
        driver_class="FakeDriver",
        driver_uri="fake:///x",
    )
    store.record_intent(kind="pool", backend_name="bn-pool", plan_name="pool1")
    store.confirm("bn-pool")
    store.record_intent(kind="network", backend_name="bn-netA", plan_name="netA")
    store.confirm("bn-netA")
    store.record_intent(kind="network", backend_name="bn-netB", plan_name="netB")
    store.confirm("bn-netB")
    store.set_phase(PHASE_RUN)
    # Simulate the owner having exited: drop its advisory lock.
    store.release()
    return run_id, store


class TestCleanupRun:
    def test_destroys_in_reverse(
        self,
        populated_state: tuple[str, StateStore],
        fake_driver: _FakeDriver,
    ) -> None:
        run_id, _ = populated_state
        result = cleanup_run(run_id)
        # Reverse order: netB, netA, pool
        assert result.destroyed == ("bn-netB", "bn-netA", "bn-pool")
        assert fake_driver.destroyed == [
            ("network", "bn-netB"),
            ("network", "bn-netA"),
            ("pool", "bn-pool"),
        ]
        assert fake_driver.connected is False

    def test_live_owner_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fake_driver: _FakeDriver,
    ) -> None:
        del fake_driver  # unused in this test
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        store = StateStore(tmp_path / "testrange" / "runs" / "r-2")
        store.initialize(
            run_id="r-2",
            plan_name="x",
            driver_class="FakeDriver",
            driver_uri="fake:///x",
        )
        # `store` still holds the advisory lock (owner alive) — cleanup must refuse.
        with pytest.raises(StateLockedError):
            cleanup_run("r-2")

    def test_dry_run(
        self,
        populated_state: tuple[str, StateStore],
        fake_driver: _FakeDriver,
    ) -> None:
        run_id, store = populated_state
        result = cleanup_run(run_id, dry_run=True)
        assert result.destroyed == ()
        assert result.skipped == ("bn-netB", "bn-netA", "bn-pool")
        assert fake_driver.destroyed == []
        # State preserved on dry-run:
        assert len(store.read().resources) == 3

    def test_missing_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        with pytest.raises(StateError):
            cleanup_run("nonexistent")

    def test_partial_failure_keeps_state(
        self,
        populated_state: tuple[str, StateStore],
        fake_driver: _FakeDriver,
    ) -> None:
        run_id, store = populated_state
        fake_driver.fail_on = {"bn-netA"}
        result = cleanup_run(run_id)
        assert "bn-netA" in {n for n, _ in result.errors}
        # The successfully-destroyed ones are gone from state
        remaining = {r.backend_name for r in store.read().resources}
        assert "bn-netA" in remaining
        assert "bn-netB" not in remaining
        assert "bn-pool" not in remaining


class TestFindRunDirs:
    def test_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert find_run_dirs() == []

    def test_lists_populated_run(
        self,
        populated_state: tuple[str, StateStore],
        tmp_path: Path,
    ) -> None:
        run_id, _ = populated_state
        dirs = find_run_dirs()
        assert [d.name for d in dirs] == [run_id]
