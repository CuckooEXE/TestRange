"""Tests for the test runner in run_tests()."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator import Orchestrator, run_tests
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockHypervisor


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default SSHCommunicator.execute to a success no-op so the new
    builder-readiness step (``cloud-init status --wait`` against
    paramiko) doesn't try a real SSH connect in unit tests."""

    def fake_execute(
        self: SSHCommunicator,
        argv: Any,
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        del self, argv, timeout, cwd
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)


@pytest.fixture
def setup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CacheManager:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    src = tmp_path / "base.qcow2"
    src.write_bytes(b"FAKE-BASE" * 50)
    cache.add(src, name="debian-13")
    sidecar = tmp_path / "sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR" * 50)
    cache.add(sidecar, name="testrange-sidecar")
    return CacheManager(local=cache)


def _plan() -> Plan:
    return Plan(
        "hello",
        MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=DHCPAddr()),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
    )


def _install_fake_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from testrange.orchestrator.backend import ResolvedBackend
    from tests.mock_driver import MockDriver

    driver = MockDriver(pool_root=tmp_path / "pools")

    def _fake_resolve(plan: Any, profile: Any) -> ResolvedBackend:
        return ResolvedBackend(
            driver=driver,
            driver_uri="",
        )

    monkeypatch.setattr("testrange.orchestrator.runtime.resolve_backend", _fake_resolve)
    return driver


class TestRunTests:
    def test_all_pass(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _install_fake_driver(monkeypatch, tmp_path)

        def test_one(orch):  # type: ignore[no-untyped-def]
            pass

        def test_two(orch):  # type: ignore[no-untyped-def]
            pass

        results = run_tests([test_one, test_two], _plan(), cache_manager=setup_env)
        assert len(results) == 2
        assert all(r.passed for r in results)
        assert {r.name for r in results} == {"test_one", "test_two"}

    def test_ready_timeout_reaches_the_orchestrator(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # CORE-100: --ready-timeout must land on agent_ready_timeout_s — a
        # nested ESXi node's sshd comes up minutes after its DHCP lease, so the
        # 120s default needs to be operator-tunable end to end.
        _install_fake_driver(monkeypatch, tmp_path)
        seen: dict[str, float] = {}
        orig = Orchestrator.__init__

        def spy(self: Orchestrator, *args: Any, **kwargs: Any) -> None:
            seen["ready"] = kwargs.get("agent_ready_timeout_s", -1.0)
            orig(self, *args, **kwargs)

        monkeypatch.setattr(Orchestrator, "__init__", spy)

        def test_one(orch):  # type: ignore[no-untyped-def]
            pass

        run_tests([test_one], _plan(), cache_manager=setup_env, ready_timeout_s=600.0)
        assert seen["ready"] == 600.0

    def test_one_fails_others_continue(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _install_fake_driver(monkeypatch, tmp_path)

        def test_one(orch):  # type: ignore[no-untyped-def]
            raise AssertionError("boom")

        def test_two(orch):  # type: ignore[no-untyped-def]
            pass

        results = run_tests([test_one, test_two], _plan(), cache_manager=setup_env)
        assert len(results) == 2
        assert results[0].name == "test_one"
        assert not results[0].passed
        assert "boom" in (results[0].error or "")
        assert results[1].name == "test_two"
        assert results[1].passed

    def test_fail_fast(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _install_fake_driver(monkeypatch, tmp_path)

        def test_one(orch):  # type: ignore[no-untyped-def]
            raise AssertionError("boom")

        ran_second = []

        def test_two(orch):  # type: ignore[no-untyped-def]
            ran_second.append(True)

        results = run_tests(
            [test_one, test_two],
            _plan(),
            cache_manager=setup_env,
            fail_fast=True,
        )
        assert len(results) == 1
        assert not results[0].passed
        assert ran_second == []

    def test_verbose_tees_test_stdout_to_testout_logger(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Under --verbose a test's prints are routed into the live tail via the
        # TESTOUT_LOGGER, tagged with the test name (CORE-6). Capture with our own
        # handler on that logger — the testrange tree has propagate=False, so a
        # root-level caplog would miss these records.
        import logging

        from testrange._tui import TESTOUT_LOGGER

        _install_fake_driver(monkeypatch, tmp_path)

        records: list[logging.LogRecord] = []

        class _Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        sink = _Collect()
        testout = logging.getLogger(TESTOUT_LOGGER)
        testout.addHandler(sink)
        testout.setLevel(logging.INFO)

        def test_chatty(orch):  # type: ignore[no-untyped-def]
            print("hello from the test")  # noqa: T201 — exercising stdout capture

        try:
            results = run_tests([test_chatty], _plan(), cache_manager=setup_env, verbose=True)
        finally:
            testout.removeHandler(sink)
        assert all(r.passed for r in results)
        assert any("hello from the test" in r.getMessage() for r in records)
        assert all(getattr(r, "tr_step", None) == "test_chatty" for r in records)

    def test_non_verbose_does_not_capture_stdout(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Without --verbose, test prints pass straight through to real stdout.
        _install_fake_driver(monkeypatch, tmp_path)

        def test_chatty(orch):  # type: ignore[no-untyped-def]
            print("straight to stdout")  # noqa: T201 — exercising stdout passthrough

        run_tests([test_chatty], _plan(), cache_manager=setup_env)
        assert "straight to stdout" in capsys.readouterr().out


class TestCommunicatorBindDuringEnter:
    def test_handle_has_communicator(
        self,
        setup_env: CacheManager,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _install_fake_driver(monkeypatch, tmp_path)
        # Stub paramiko on the SSH side so bind succeeds without trying to connect
        # (bind itself doesn't connect — it just stores host + credential).
        with Orchestrator(_plan(), cache_manager=setup_env) as orch:
            assert "web" in orch.vms
            handle = orch.vms["web"]
            assert isinstance(handle.communicator, SSHCommunicator)
            assert handle.communicator.is_bound
            # DHCP-discovered from the sidecar lease file, in the switch subnet.
            assert (handle.communicator.host or "").startswith("10.0.1.")
