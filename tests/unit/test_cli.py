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
        assert "Plan (LibvirtHypervisor)" in out
        assert "qemu:///session" in out
        assert "switch1" in out
        assert "netA" in out
        assert "pool1" in out
        assert "web" in out
        assert "debian-13" in out
        assert "⚠ not in cache" in out  # cache resolution attempted, miss surfaced
        assert "cloud_init_finished" in out

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
