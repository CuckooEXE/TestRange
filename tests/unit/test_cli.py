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
    def test_describe_hello_world(self, capsys: pytest.CaptureFixture[str]) -> None:
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
        assert "Phase 1" in out  # cache resolution warning
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


class TestStubSubcommands:
    @pytest.mark.parametrize("sub", ["cache", "run", "cleanup"])
    def test_stub(self, sub: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main([sub])
        assert rc == 2
        err = capsys.readouterr().err
        assert "not implemented" in err
