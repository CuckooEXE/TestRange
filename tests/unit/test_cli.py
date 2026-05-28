"""Tests for the CLI entry point + describe subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import cli

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


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
        assert "Plan (ProxmoxHypervisor)" in out
        assert "switch1" in out
        assert "netA" in out
        assert "pool1" in out
        assert "web" in out
        assert "debian-13" in out
        assert "⚠ not in cache" in out  # cache resolution attempted, miss surfaced
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


class TestTestsValidation:
    """_load_plan_module rejects a malformed TESTS up front (exit 2)."""

    def _write(self, tmp_path: Path, tests_src: str) -> Path:
        f = tmp_path / "plan.py"
        f.write_text(
            "from testrange import Plan\n"
            "from testrange.drivers.mock import MockHypervisor\n"
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
