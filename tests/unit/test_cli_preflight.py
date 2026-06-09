"""The ``testrange preflight`` verb (CORE-85): connect, run every check, print
each result, exit non-zero on a blocker. Driven against the MockDriver with a
patched backend binding so no real host is touched."""

from __future__ import annotations

import pytest

from testrange import Plan, cli
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.backend import ResolvedBackend
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor


def _plan() -> Plan:
    return Plan(
        "pf",
        MockHypervisor(
            networks=[Switch("sw", Network("n"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))],
            pools=[StoragePool("pool1", 16)],
            vms=[
                VMRecipe(
                    spec=VMSpec(name="vm", devices=[CPU(1), Memory(512), OSDrive("pool1", 8)]),
                    builder=CloudInitBuilder(base=CacheEntry("debian-13")),
                    communicator=SSHCommunicator("u"),
                )
            ],
        ),
    )


def _bind(monkeypatch: pytest.MonkeyPatch, driver: MockDriver) -> None:
    monkeypatch.setattr(cli, "_load_plan_module", lambda _path: (_plan(), []))
    monkeypatch.setattr(
        cli,
        "resolve_backend",
        lambda _plan, _profile: ResolvedBackend(driver=driver, driver_uri=""),
    )


def test_preflight_clean_exits_zero_and_lists_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(
        monkeypatch, MockDriver(backing_memory_mb=65_536, backing_cpus=16, backing_capacity_gb=128)
    )
    rc = cli.main(["preflight", "plan.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "preflight: clean" in out
    assert "[ OK ] host-resources" in out
    assert "[ OK ] named-uplink-resolution" in out


def test_preflight_blocks_impossible_memory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(monkeypatch, MockDriver(backing_memory_mb=256))  # VM asks for 512 MiB
    rc = cli.main(["preflight", "plan.py"])
    out = capsys.readouterr().out
    assert rc == 2  # USAGE: a blocker
    assert "[FAIL] host-resources" in out
    assert "insufficient-memory" in out


def test_preflight_skips_resource_gate_when_capacity_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _bind(monkeypatch, MockDriver())  # no backing_* knobs -> host_capacity() is None
    rc = cli.main(["preflight", "plan.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[SKIP] host-resources" in out


def test_preflight_without_profile_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No binding patch: the real resolver rejects a pinned plan with no --profile.
    monkeypatch.setattr(cli, "_load_plan_module", lambda _path: (_plan(), []))
    rc = cli.main(["preflight", "plan.py"])
    assert rc == 2
    assert "--profile" in capsys.readouterr().err


def test_preflight_connect_failure_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = MockDriver(backing_memory_mb=4096, backing_cpus=4)

    def _boom() -> None:
        raise DriverError("cannot reach host")

    monkeypatch.setattr(driver, "connect", _boom)
    _bind(monkeypatch, driver)
    rc = cli.main(["preflight", "plan.py"])
    assert rc == 2
    assert "cannot reach host" in capsys.readouterr().err


def test_preflight_disconnects_even_when_preflight_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = MockDriver(backing_memory_mb=4096, backing_cpus=4)

    def _boom(*_a: object, **_k: object) -> None:
        raise DriverError("backend exploded")

    monkeypatch.setattr(driver, "preflight", _boom)
    _bind(monkeypatch, driver)
    rc = cli.main(["preflight", "plan.py"])
    assert rc == 2
    assert driver.connected is False  # disconnect ran in the finally
    assert "backend exploded" in capsys.readouterr().err


def test_preflight_interrupt_returns_interrupted_and_disconnects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = MockDriver(backing_memory_mb=4096, backing_cpus=4)

    def _interrupt(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(driver, "preflight", _interrupt)
    _bind(monkeypatch, driver)
    rc = cli.main(["preflight", "plan.py"])
    assert rc == 130  # Exit.INTERRUPTED
    assert driver.connected is False  # disconnect still ran
    assert "interrupted" in capsys.readouterr().err
