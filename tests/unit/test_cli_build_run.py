"""CLI `build` / `run` split + auto-build (ADR-0010 §1, Phase B5).

Four paths, end-to-end through ``cli.main`` against the MockDriver:

1. ``build`` warms the cache and creates no run VMs.
2. ``run`` after a ``build`` is a pure warm-cache bring-up (no build VM).
3. ``run`` against a cold cache auto-builds, then runs.
4. ``run --require-cache`` against a cold cache exits non-zero ("build first").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testrange import cli
from testrange.cache import CacheManager, LocalCache
from testrange.communicators import ExecResult, SSHCommunicator
from testrange.orchestrator import run_tests
from testrange.orchestrator.backend import ResolvedBackend
from tests.mock_driver import MockDriver

_PLAN_SRC = """
from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from tests.mock_driver import MockHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

hyp = MockHypervisor()
hyp.add_pool(StoragePool("pool1", 32))
hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True)))
hyp.add_vm(VMRecipe(
    spec=VMSpec(name="web", devices=[
        CPU(1), Memory(512), OSDrive(hyp.pools["pool1"], 8),
        NetworkIface(hyp.networks["netA"], addr=DHCPAddr()),
    ]),
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
    ),
    communicator=SSHCommunicator("u"),
))
PLAN = Plan("hello", hyp)

def test_ok(orch):
    pass

TESTS = [test_ok]
"""

# A VM with a data disk, on the Mock backend, for the build->capture->run-import
# lifecycle assertions. Kept inline (not loaded from an example) so the Mock
# coverage doesn't depend on an example's backend.
_DATA_DISK_PLAN_SRC = """
from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from tests.mock_driver import MockHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

hyp = MockHypervisor()
hyp.add_pool(StoragePool("pool1", 64))
hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True)))
hyp.add_vm(VMRecipe(
    spec=VMSpec(name="fileserver", devices=[
        CPU(1), Memory(512), OSDrive(hyp.pools["pool1"], 8), HardDrive(hyp.pools["pool1"], 16),
        NetworkIface(hyp.networks["netA"], addr=DHCPAddr()),
    ]),
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
    ),
    communicator=SSHCommunicator("u"),
))
PLAN = Plan("data-disk-mock", hyp)

def test_ok(orch):
    pass

TESTS = [test_ok]
"""


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute(
        self: SSHCommunicator, argv: Any, *, timeout: float = 60.0, cwd: str | None = None
    ) -> ExecResult:
        del self, argv, timeout, cwd
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[MockDriver, str]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"FAKE-BASE" * 50)
    cache.add(base, name="debian-13")
    sidecar = tmp_path / "sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR" * 50)
    cache.add(sidecar, name="testrange-sidecar")

    driver = MockDriver(pool_root=tmp_path / "pools")

    def _fake_resolve(plan: object, profile: object) -> ResolvedBackend:
        return ResolvedBackend(
            driver=driver,
            driver_uri="",
        )

    monkeypatch.setattr("testrange.orchestrator.runtime.resolve_backend", _fake_resolve)

    plan_path = tmp_path / "plan.py"
    plan_path.write_text(_PLAN_SRC)
    return driver, str(plan_path)


def _build_vm_creates(driver: MockDriver) -> int:
    return sum(1 for c in driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0])


def _run_vm_creates(driver: MockDriver) -> int:
    return sum(1 for c in driver.calls if c[0] == "create_vm" and c[1][0].startswith("tr_vm_"))


class TestBuildVerb:
    def test_build_warms_cache_no_run_vm(
        self, env: tuple[MockDriver, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        driver, plan_path = env
        rc = cli.main(["build", plan_path])
        assert rc == 0
        assert "build complete" in capsys.readouterr().out
        # A build VM ran; no run VM was ever created.
        assert _build_vm_creates(driver) == 1
        assert _run_vm_creates(driver) == 0

    def test_build_then_run_is_warm_hit(self, env: tuple[MockDriver, str]) -> None:
        driver, plan_path = env
        assert cli.main(["build", plan_path]) == 0
        driver.calls = []
        assert cli.main(["run", plan_path]) == 0
        # Warm cache: run creates the run VM but builds nothing.
        assert _build_vm_creates(driver) == 0
        assert _run_vm_creates(driver) == 1


class TestVerboseFlag:
    def test_run_verbose_end_to_end(self, env: tuple[MockDriver, str]) -> None:
        # --verbose is a global flag (before the subcommand). On a non-tty
        # (pytest capture) run_dashboard takes its plain-logging path; the run
        # still completes normally (CORE-6, ADR-0029).
        driver, plan_path = env
        rc = cli.main(["--verbose", "run", plan_path])
        assert rc == 0
        assert _build_vm_creates(driver) == 1
        assert _run_vm_creates(driver) == 1

    def test_build_verbose_end_to_end(self, env: tuple[MockDriver, str]) -> None:
        driver, plan_path = env
        assert cli.main(["--verbose", "build", plan_path]) == 0
        assert _build_vm_creates(driver) == 1

    def test_verbose_in_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        assert "--verbose" in capsys.readouterr().out


class TestRunAutoBuild:
    def test_run_cold_cache_auto_builds(self, env: tuple[MockDriver, str]) -> None:
        driver, plan_path = env
        # Cold cache (no `build` first): run must auto-build then bring up.
        rc = cli.main(["run", plan_path])
        assert rc == 0
        assert _build_vm_creates(driver) == 1
        assert _run_vm_creates(driver) == 1

    def test_require_cache_cold_fails_fast(
        self, env: tuple[MockDriver, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        driver, plan_path = env
        rc = cli.main(["run", "--require-cache", plan_path])
        assert rc == 2
        assert "cache miss" in capsys.readouterr().err
        # Nothing was built and no run VM started — it failed before bring-up.
        assert _build_vm_creates(driver) == 0
        assert _run_vm_creates(driver) == 0

    def test_require_cache_warm_succeeds(self, env: tuple[MockDriver, str]) -> None:
        driver, plan_path = env
        assert cli.main(["build", plan_path]) == 0
        driver.calls = []
        assert cli.main(["run", "--require-cache", plan_path]) == 0
        assert _build_vm_creates(driver) == 0
        assert _run_vm_creates(driver) == 1


class TestDataDiskLifecycle:
    """A data-disk plan brings up green on the mock driver: blank-at-build,
    captured, re-imported at run."""

    def test_data_disk_lifecycle(self, env: tuple[MockDriver, str], tmp_path: Path) -> None:
        driver, _plan_path = env
        plan_path = tmp_path / "data_disk_mock.py"
        plan_path.write_text(_DATA_DISK_PLAN_SRC)
        plan, tests = cli._load_plan_module(str(plan_path))
        results = run_tests(tests, plan, cache_manager=CacheManager())
        assert results and all(r.passed for r in results)
        # The data disk was built blank, captured, and pushed back at run.
        assert any(c[0] == "create_blank_volume" for c in driver.calls)
        run_data_uploads = [
            c
            for c in driver.calls
            if c[0] == "upload_to_pool" and "tr_vm_" in c[1][0] and c[1][0].endswith("-data0.qcow2")
        ]
        assert len(run_data_uploads) == 1


class TestResultReporting:
    """`[PASS]`/`[FAIL]` report lines are gated on the dashboard being inactive
    (CORE-83): the live dashboard already shows pass/fail, so a redundant dump
    below the final frame is suppressed when it was active — but failures (which
    the tests panel renders without their error) still print."""

    def test_no_dashboard_prints_pass_lines(
        self, env: tuple[MockDriver, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _driver, plan_path = env
        # Non-tty under pytest capture, so the dashboard is inactive regardless;
        # --no-dashboard makes the intent explicit. The PASS line is the output.
        assert cli.main(["--no-dashboard", "run", plan_path]) == 0
        assert "[PASS] test_ok" in capsys.readouterr().out

    def test_active_dashboard_suppresses_pass_lines(
        self,
        env: tuple[MockDriver, str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import io

        from rich.console import Console

        _driver, plan_path = env
        # Force the dashboard active by giving cli a tty- like stderr console.
        term = Console(file=io.StringIO(), force_terminal=True)
        monkeypatch.setattr(cli, "err_console", lambda: term)
        assert cli.main(["run", plan_path]) == 0
        # The passing test's report line is suppressed on stdout (the dashboard,
        # on stderr, already showed it).
        assert "[PASS]" not in capsys.readouterr().out


class TestBuildParser:
    def test_build_requires_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["build"])
        assert "required" in capsys.readouterr().err

    def test_run_help_shows_require_cache(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["run", "--help"])
        assert "--require-cache" in capsys.readouterr().out
