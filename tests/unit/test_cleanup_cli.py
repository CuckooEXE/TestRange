"""Tests for the `testrange cleanup` CLI subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import cli
from testrange.state.store import StateStore


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "testrange" / "runs"


class _FakeDriver:
    DRIVER_NAME = "FakeDriver"

    def __init__(self, *, uri: str) -> None:
        self.uri = uri
        self.destroyed: list[tuple[str, str]] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def destroy(self, kind: str, backend_name: str) -> None:
        self.destroyed.append((kind, backend_name))


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> _FakeDriver:
    d = _FakeDriver(uri="fake:///x")

    def _instantiate(cls: str, uri: str) -> _FakeDriver:
        d.uri = uri
        return d

    monkeypatch.setattr("testrange.state.cleanup._instantiate_driver", _instantiate)
    return d


def _populate_run(runs_root: Path, run_id: str = "r-test") -> StateStore:
    store = StateStore(runs_root / run_id)
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
    store.pid_path.write_text("0\n")
    return store


class TestCleanupCLI:
    def test_cleanup_run_dry(
        self,
        isolated_state: Path,
        fake_driver: _FakeDriver,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-1")
        rc = cli.main(["cleanup", "r-1", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "would destroy" in out
        assert fake_driver.destroyed == []

    def test_cleanup_run_real(
        self,
        isolated_state: Path,
        fake_driver: _FakeDriver,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-1")
        rc = cli.main(["cleanup", "r-1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "destroyed" in out
        assert ("network", "bn-netA") in fake_driver.destroyed

    def test_cleanup_all(
        self,
        isolated_state: Path,
        fake_driver: _FakeDriver,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-1")
        _populate_run(isolated_state, "r-2")
        rc = cli.main(["cleanup", "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "r-1" in out and "r-2" in out

    def test_cleanup_missing_run(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup", "nope"])
        assert rc == 2
        assert "no state" in capsys.readouterr().err

    def test_cleanup_locked_pid(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import os

        store = _populate_run(isolated_state, "r-locked")
        store.pid_path.write_text(f"{os.getpid()}\n")
        rc = cli.main(["cleanup", "r-locked"])
        assert rc == 1
        assert "still owns" in capsys.readouterr().err

    def test_cleanup_no_args(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup"])
        assert rc == 2
        assert "requires" in capsys.readouterr().err
