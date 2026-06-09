"""Tests for the CLI entry point + describe subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import cli

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PLANS = Path(__file__).resolve().parents[1] / "plans"


class TestVersion:
    def test_version_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "testrange" in captured.out


class TestDescribe:
    def test_describe_hello_world(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Isolate the cache so the describe output is deterministic for tests.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        rc = cli.main(["describe", str(EXAMPLES / "hello_world.py")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Plan (Hypervisor)" in out  # portable plan (ADR-0015)
        assert "backend: UNBOUND" in out
        assert "switch1" in out
        assert "netA" in out
        assert "pool1" in out
        assert "web" in out
        assert "debian-13" in out
        assert "(!) not in cache" in out  # cache resolution attempted, miss surfaced
        assert "nginx_is_installed" in out

    def test_describe_missing_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", "/nonexistent/plan.py"])
        assert exc.value.code == 2

    def test_describe_no_plan_var(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        f = tmp_path / "empty.py"
        f.write_text("x = 1\n")
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", str(f)])
        assert exc.value.code == 2

    def test_plan_that_raises_on_import_is_usage_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # B4: a plan is arbitrary user .py; an exception at import (here a bare
        # ValueError, as topology validation raises) must surface as a usage
        # error on stderr, not a raw traceback.
        f = tmp_path / "boom.py"
        f.write_text("raise ValueError('bad topology')\n")
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", str(f)])
        assert exc.value.code == 2
        assert "boom.py" in capsys.readouterr().err

    def test_describe_binding_error_is_nonzero_and_on_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # H13: a profile that fails to resolve must exit non-zero with the error
        # on stderr (not stdout/exit-0), so `describe && run` stops on a broken
        # binding instead of proceeding.
        from testrange.exceptions import DriverError

        def _boom(*_a: object, **_k: object) -> object:
            raise DriverError("incompatible binding")

        monkeypatch.setattr("testrange.cli.resolve_backend", _boom)
        plan_path = _write_generic_plan(tmp_path)
        prof = tmp_path / "connect.toml"
        prof.write_text('[p]\ndriver = "mock"\n')
        rc = cli.main(["describe", plan_path, "--profile", f"{prof}:p"])
        assert rc == 2
        out = capsys.readouterr()
        assert "incompatible binding" in out.err
        assert "ERROR" not in out.out


class TestRunSubcommand:
    def test_requires_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["run"])
        err = capsys.readouterr().err
        assert "required" in err

    def test_help_shows_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["run", "--help"])
        out = capsys.readouterr().out
        assert "--fail-fast" in out
        assert "--leak-on-failure" in out


class TestReplSubcommand:
    def test_requires_plan(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["repl"])
        err = capsys.readouterr().err
        assert "required" in err

    def test_help_mentions_orch_and_leak(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            cli.main(["repl", "--help"])
        out = capsys.readouterr().out
        assert "orch" in out
        assert "leak" in out

    def test_missing_plan_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Same contract as `describe` and `run`: a nonexistent plan file
        # produces exit 2 from _load_plan_module before any bring-up.
        with pytest.raises(SystemExit) as exc:
            cli.main(["repl", "/nonexistent/plan.py"])
        assert exc.value.code == 2


_GENERIC_PLAN_SRC = """
from testrange import Hypervisor, Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "portable",
    Hypervisor(
        networks=[Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(name="web", devices=[CPU(1), Memory(512), OSDrive("pool1", 8),
                                                 NetworkIface("netA")]),
                builder=CloudInitBuilder(base=CacheEntry("debian-13"),
                                         credentials=[PosixCred("u", password="p")]),
                communicator=SSHCommunicator("u"),
            ),
        ],
    ),
)

def t(orch):
    pass

TESTS = [t]
"""


def _write_generic_plan(tmp_path: Path) -> str:
    p = tmp_path / "portable_plan.py"
    p.write_text(_GENERIC_PLAN_SRC)
    return str(p)


class TestConnectFlag:
    def test_run_passes_profile_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plan_path = _write_generic_plan(tmp_path)
        prof = tmp_path / "connect.toml"
        prof.write_text('[p]\ndriver = "mock"\n')

        captured: dict[str, object] = {}

        def fake_run_tests(tests: object, plan: object, **kwargs: object) -> list[object]:
            captured["profile"] = kwargs.get("profile")
            return []

        monkeypatch.setattr(cli, "run_tests", fake_run_tests)
        rc = cli.main(["run", plan_path, "--profile", f"{prof}:p"])
        assert rc == 0
        from testrange.connect import BackendProfile

        assert isinstance(captured["profile"], BackendProfile)
        assert captured["profile"].scheme == "mock"

    def test_build_passes_profile_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plan_path = _write_generic_plan(tmp_path)
        prof = tmp_path / "connect.toml"
        prof.write_text('[p]\ndriver = "mock"\n')
        captured: dict[str, object] = {}

        def fake_build_range(plan: object, **kwargs: object) -> str:
            captured["profile"] = kwargs.get("profile")
            return "run-xyz"

        monkeypatch.setattr(cli, "build_range", fake_build_range)
        rc = cli.main(["build", plan_path, "--profile", f"{prof}:p"])
        assert rc == 0
        assert captured["profile"] is not None

    def test_run_without_connect_passes_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plan_path = _write_generic_plan(tmp_path)
        captured: dict[str, object] = {}

        def fake_run_tests(tests: object, plan: object, **kwargs: object) -> list[object]:
            captured["profile"] = kwargs.get("profile", "MISSING")
            return []

        monkeypatch.setattr(cli, "run_tests", fake_run_tests)
        rc = cli.main(["run", plan_path])
        assert rc == 0
        assert captured["profile"] is None

    def test_profile_not_found_exits_2(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        plan_path = _write_generic_plan(tmp_path)
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", plan_path, "--profile", f"{tmp_path / 'nope.toml'}:p"])
        assert exc.value.code == 2
        assert "not found" in capsys.readouterr().err

    def test_generic_plan_no_connect_run_errors_exit_2(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
        plan_path = _write_generic_plan(tmp_path)
        rc = cli.main(["run", plan_path])
        assert rc == 2
        assert "backend-agnostic" in capsys.readouterr().err


class TestDescribeBinding:
    def test_generic_unbound_without_connect(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        rc = cli.main(["describe", _write_generic_plan(tmp_path)])
        assert rc == 0
        assert "backend: UNBOUND" in capsys.readouterr().out

    def test_generic_with_connect_shows_masked_binding(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        plan_path = _write_generic_plan(tmp_path)
        prof = tmp_path / "connect.toml"
        prof.write_text('[p]\ndriver = "proxmox"\nhost = "10.0.0.5"\npassword = "Secret123!"\n')
        rc = cli.main(["describe", plan_path, "--profile", f"{prof}:p"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "driver: proxmox (ProxmoxDriver)" in out
        assert "host: 10.0.0.5" in out
        assert "password: ***set***" in out
        assert "Secret123!" not in out  # never printed

    def test_concrete_plan_shows_pinned_unbound_without_connect(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        # CORE-19: plans/proxmox/devices.py is scheme-pinned (ProxmoxHypervisor) but carries
        # no connection, so describe without --profile renders an UNBOUND binding
        # that names the pinned scheme so the dev knows which profile to point at.
        rc = cli.main(["describe", str(PLANS / "proxmox" / "devices.py")])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Plan (ProxmoxHypervisor)" in out
        assert "backend: UNBOUND (pinned to 'proxmox'" in out
        assert "--profile <proxmox-profile>" in out

    def test_concrete_plan_with_matching_connect_shows_binding(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        prof = tmp_path / "connect.toml"
        prof.write_text('[p]\ndriver = "proxmox"\nhost = "10.0.0.5"\npassword = "Secret123!"\n')
        rc = cli.main(["describe", str(PLANS / "proxmox" / "devices.py"), "--profile", f"{prof}:p"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Plan (ProxmoxHypervisor)" in out
        assert "driver: proxmox (ProxmoxDriver)" in out
        assert "host: 10.0.0.5" in out
        assert "password: ***set***" in out
        assert "Secret123!" not in out  # masked, value not shown


class TestTestsValidation:
    """_load_plan_module rejects a malformed TESTS up front (exit 2)."""

    def _write(self, tmp_path: Path, tests_src: str) -> Path:
        f = tmp_path / "plan.py"
        f.write_text(
            "from testrange import Plan\n"
            "from tests.mock_driver import MockHypervisor\n"
            'PLAN = Plan("t", MockHypervisor())\n'
            f"{tests_src}\n"
        )
        return f

    def test_valid_tests_ok(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = self._write(tmp_path, "def t(orch):\n    pass\nTESTS = [t]")
        assert cli.main(["describe", str(f)]) == 0

    def test_not_a_list(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = self._write(tmp_path, "TESTS = 'nope'")
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", str(f)])
        assert exc.value.code == 2
        assert "must be a list" in capsys.readouterr().err

    def test_entry_not_callable(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = self._write(tmp_path, "TESTS = [123]")
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", str(f)])
        assert exc.value.code == 2
        assert "not callable" in capsys.readouterr().err

    def test_entry_wrong_arity(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = self._write(tmp_path, "def t():\n    pass\nTESTS = [t]")
        with pytest.raises(SystemExit) as exc:
            cli.main(["describe", str(f)])
        assert exc.value.code == 2
        assert "one argument" in capsys.readouterr().err
