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
    # Caller decides ownership: leave the advisory lock held (owner alive) or
    # call store.release() to simulate the owner having exited.
    return store


class TestForget:
    """cleanup --forget drops bookkeeping without touching any backend (ORCH-40)."""

    def test_forget_drops_ledger_without_a_driver(
        self,
        isolated_state: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The whole point: the backend may be permanently gone, so forgetting
        # must never instantiate or connect a driver.
        def _boom(cls: str, uri: str) -> None:
            raise AssertionError("forget must not touch the driver registry")

        monkeypatch.setattr("testrange.state.cleanup._instantiate_driver", _boom)
        store = _populate_run(isolated_state, "r-forget")
        store.release()
        rc = cli.main(["cleanup", "--forget", "r-forget"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "backend untouched" in out
        assert "bn-pool" in out and "bn-netA" in out  # loud: every entry named
        assert not (isolated_state / "r-forget").exists()

    def test_forget_refuses_a_live_owner(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-live")  # advisory lock still held
        rc = cli.main(["cleanup", "--forget", "r-live"])
        assert rc == 1
        assert "owned by a live process" in capsys.readouterr().err
        assert (isolated_state / "r-live").exists()

    def test_forget_requires_a_run_id(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup", "--forget"])
        assert rc == 2
        assert "requires <run-id>" in capsys.readouterr().err

    def test_forget_missing_run_errors(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup", "--forget", "nope"])
        assert rc == 2
        assert "no run dir" in capsys.readouterr().err

    @pytest.mark.parametrize("extra", ["--all", "--list", "--dry-run"])
    def test_forget_rejects_other_mode_flags(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
        extra: str,
    ) -> None:
        # Forgetting is deliberate and per-run; a sweep or dry-run combination
        # would blur exactly the explicitness that makes it safe.
        store = _populate_run(isolated_state, "r-combo")
        store.release()
        rc = cli.main(["cleanup", "--forget", "r-combo", extra])
        assert rc == 2
        assert "no other mode flags" in capsys.readouterr().err
        assert (isolated_state / "r-combo").exists()

    def test_forget_tolerates_corrupt_state(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Disposing of broken bookkeeping is part of the point: a corrupt
        # state.json must not block the drop (cleanup_run would refuse).
        store = _populate_run(isolated_state, "r-corrupt")
        store.release()
        store.state_path.write_text("{not json")
        rc = cli.main(["cleanup", "--forget", "r-corrupt"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "0 ledger entries dropped" in out
        assert not (isolated_state / "r-corrupt").exists()


class TestCleanupCLI:
    def test_cleanup_run_dry(
        self,
        isolated_state: Path,
        fake_driver: _FakeDriver,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-1").release()
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
        _populate_run(isolated_state, "r-1").release()
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
        _populate_run(isolated_state, "r-1").release()
        _populate_run(isolated_state, "r-2").release()
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

    def test_cleanup_locked_live_owner(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Owner still alive: _populate_run leaves the advisory lock held, so
        # cleanup must refuse rather than tear down a live run.
        _populate_run(isolated_state, "r-locked")
        rc = cli.main(["cleanup", "r-locked"])
        assert rc == 1
        assert "owned by a live process" in capsys.readouterr().err

    def test_cleanup_no_args(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup"])
        assert rc == 2
        assert "requires" in capsys.readouterr().err

    def test_cleanup_list_empty(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        del isolated_state
        rc = cli.main(["cleanup", "--list"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "(no runs)"

    def test_cleanup_list_status_and_metadata(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # r-stopped: owner exited (lock released). r-live: owner still holds the
        # advisory lock. --list must distinguish them and never touch resources.
        _populate_run(isolated_state, "r-stopped").release()
        _populate_run(isolated_state, "r-live")
        rc = cli.main(["cleanup", "--list"])
        assert rc == 0
        out = capsys.readouterr().out
        lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("r-")}
        assert "stopped" in lines["r-stopped"]
        assert "running" in lines["r-live"]
        # plan name and resource count surface for each run.
        assert "hello" in lines["r-stopped"]
        assert lines["r-stopped"].split()[-2] == "2"

    def test_cleanup_list_tolerates_corrupt_state(
        self,
        isolated_state: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _populate_run(isolated_state, "r-ok").release()
        bad = isolated_state / "r-bad"
        bad.mkdir(parents=True)
        (bad / "state.json").write_text("{not json", encoding="utf-8")
        rc = cli.main(["cleanup", "--list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "r-ok" in out and "r-bad" in out
        assert "error:" in out
