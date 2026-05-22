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
from testrange.drivers.mock import MockDriver
from testrange.orchestrator import run_tests
from testrange.orchestrator.runtime import Orchestrator

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

_PLAN_SRC = '''
from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.mock import MockHypervisor
from testrange.networks import Network, Switch
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    MockHypervisor(
        networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24", dhcp=True, dns=True)],
        pools=[StoragePool("pool1", 32)],
        vms=[VMRecipe(
            spec=VMSpec(name="web", devices=[
                CPU(1), Memory(512), OSDrive("pool1", 8),
                NetworkIface("netA", addr=DHCPAddr()),
            ]),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
            ),
            communicator=SSHCommunicator("u"),
        )],
    ),
    name="hello",
)

def test_ok(orch):
    pass

TESTS = [test_ok]
'''


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
    monkeypatch.setattr(Orchestrator, "_build_driver", lambda self: driver)

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

    def test_build_then_run_is_warm_hit(
        self, env: tuple[MockDriver, str]
    ) -> None:
        driver, plan_path = env
        assert cli.main(["build", plan_path]) == 0
        driver.calls = []
        assert cli.main(["run", plan_path]) == 0
        # Warm cache: run creates the run VM but builds nothing.
        assert _build_vm_creates(driver) == 0
        assert _run_vm_creates(driver) == 1


class TestRunAutoBuild:
    def test_run_cold_cache_auto_builds(
        self, env: tuple[MockDriver, str]
    ) -> None:
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

    def test_require_cache_warm_succeeds(
        self, env: tuple[MockDriver, str]
    ) -> None:
        driver, plan_path = env
        assert cli.main(["build", plan_path]) == 0
        driver.calls = []
        assert cli.main(["run", "--require-cache", plan_path]) == 0
        assert _build_vm_creates(driver) == 0
        assert _run_vm_creates(driver) == 1


class TestDataDiskExample:
    """The canonical data-disk example brings up green on the mock driver."""

    def test_example_runs_green(self, env: tuple[MockDriver, str]) -> None:
        driver, _plan_path = env
        plan, tests = cli._load_plan_module(str(EXAMPLES / "data_disk.py"))
        results = run_tests(tests, plan, cache_manager=CacheManager())
        assert results and all(r.passed for r in results)
        # The data disk was built blank, captured, and pushed back at run.
        assert any(c[0] == "create_blank_volume" for c in driver.calls)
        run_data_uploads = [
            c
            for c in driver.calls
            if c[0] == "upload_to_pool"
            and "tr_vm_" in c[1][0]
            and c[1][0].endswith("-data0.qcow2")
        ]
        assert len(run_data_uploads) == 1


class TestBuildParser:
    def test_build_requires_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["build"])
        assert "required" in capsys.readouterr().err

    def test_run_help_shows_require_cache(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["run", "--help"])
        assert "--require-cache" in capsys.readouterr().out
