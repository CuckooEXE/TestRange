"""Tests for state.cleanup — reverse-walking state.json + flock ownership gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.exceptions import DriverError, StateError, StateLockedError
from testrange.state.cleanup import cleanup_all, cleanup_run, find_run_dirs
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
        self.fail_connect = False  # simulate a vanished/unreachable backend

    def connect(self) -> None:
        if self.fail_connect:
            raise DriverError(f"cannot reach backend at {self.uri}")
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


class TestCleanupAll:
    def test_dead_backend_does_not_abort_the_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A run whose backend is gone (connect() raises) must not stop the
        others — cleanup_all attempts every state file independently (CORE-59)."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

        def _make_run(run_id: str, uri: str) -> None:
            store = StateStore(tmp_path / "testrange" / "runs" / run_id)
            store.initialize(
                run_id=run_id, plan_name="p", driver_class="FakeDriver", driver_uri=uri
            )
            store.record_intent(kind="pool", backend_name=f"{run_id}-pool", plan_name="pool1")
            store.confirm(f"{run_id}-pool")
            store.set_phase(PHASE_RUN)
            store.release()  # owner exited — eligible for cleanup

        # find_run_dirs sorts, so "r-1-dead" is walked before "r-2-live": if the
        # dead backend aborted the sweep, the live run would never be reached.
        _make_run("r-1-dead", "fake:///dead")
        _make_run("r-2-live", "fake:///live")

        live = _FakeDriver(uri="fake:///live")

        def _instantiate(cls: str, uri: str) -> _FakeDriver:
            if uri == "fake:///dead":
                dead = _FakeDriver(uri=uri)
                dead.fail_connect = True
                return dead
            return live

        monkeypatch.setattr("testrange.state.cleanup._instantiate_driver", _instantiate)

        results = {r.run_id: r for r in cleanup_all()}

        # Both runs were attempted — the dead one did not abort the sweep.
        assert set(results) == {"r-1-dead", "r-2-live"}
        # Dead run: nothing destroyed, the failure recorded, ledger preserved.
        assert results["r-1-dead"].destroyed == ()
        assert results["r-1-dead"].errors
        assert StateStore(tmp_path / "testrange" / "runs" / "r-1-dead").exists()
        # Live run: cleaned through to completion despite the earlier failure.
        assert results["r-2-live"].destroyed == ("r-2-live-pool",)
        assert live.destroyed == [("pool", "r-2-live-pool")]


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
