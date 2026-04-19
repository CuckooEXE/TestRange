"""Unit tests for :mod:`testrange._cli`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from testrange._cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestMainGroup:
    def test_help(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["--help"])
        assert r.exit_code == 0
        assert "testrange" in r.output.lower()

    def test_version(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["--version"])
        assert r.exit_code == 0
        assert "testrange" in r.output.lower()


class TestRunCommand:
    def test_missing_separator(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["run", "justmodule"])
        assert r.exit_code == 2
        assert "module:factory" in r.output

    def test_empty_factory_name(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["run", "mymodule:"])
        assert r.exit_code == 2

    def test_missing_file_path(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["run", "/nonexistent/file.py:gen_tests"])
        assert r.exit_code == 1
        assert "not found" in r.output.lower()

    def test_factory_attribute_missing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "bad.py"
        f.write_text("# no gen_tests function\n")
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 1
        assert "gen_tests" in r.output

    def test_factory_not_callable(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "bad.py"
        f.write_text("gen_tests = 42\n")
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 1
        assert "callable" in r.output

    def test_factory_returns_non_list(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def gen_tests():\n    return 'not a list'\n")
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 1
        assert "list[Test]" in r.output

    def test_factory_returns_non_test_items(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def gen_tests():\n    return [1, 2, 3]\n")
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 1

    def test_factory_returns_empty_list(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "empty.py"
        f.write_text("def gen_tests():\n    return []\n")
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 0

    def test_factory_returns_passing_tests(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "ok.py"
        f.write_text(
            "from unittest.mock import MagicMock\n"
            "from testrange.test import Test, TestResult\n"
            "\n"
            "def _passing():\n"
            "    t = MagicMock(spec=Test)\n"
            "    t.name = 'fake'\n"
            "    t.run.return_value = TestResult(passed=True, error=None, duration=0.01)\n"
            "    return t\n"
            "\n"
            "def gen_tests():\n"
            "    return [_passing()]\n"
        )
        r = runner.invoke(main, ["run", f"{f}:gen_tests", "--quiet"])
        assert r.exit_code == 0

    def test_factory_returns_failing_tests_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "bad.py"
        f.write_text(
            "from unittest.mock import MagicMock\n"
            "from testrange.test import Test, TestResult\n"
            "\n"
            "def _failing():\n"
            "    t = MagicMock(spec=Test)\n"
            "    t.name = 'fake'\n"
            "    t.run.return_value = TestResult(\n"
            "        passed=False, error=RuntimeError('x'),\n"
            "        duration=0.01, traceback_str='Traceback ...',\n"
            "    )\n"
            "    return t\n"
            "\n"
            "def gen_tests():\n"
            "    return [_failing()]\n"
        )
        r = runner.invoke(main, ["run", f"{f}:gen_tests"])
        assert r.exit_code == 1

    def test_dotted_module_name(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg_file = tmp_path / "_cli_dotted_fixture.py"
        pkg_file.write_text("def gen_tests():\n    return []\n")
        monkeypatch.chdir(tmp_path)
        r = runner.invoke(main, ["run", "_cli_dotted_fixture:gen_tests"])
        assert r.exit_code == 0


class TestCacheList:
    def test_empty_cache(self, runner: CliRunner, tmp_path: Path) -> None:
        r = runner.invoke(main, ["cache-list", "--cache-dir", str(tmp_path)])
        assert r.exit_code == 0
        assert "Cache root" in r.output

    def test_lists_downloaded_image_meta(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Pre-seed the cache with a meta file
        cache = tmp_path / "c"
        (cache / "images").mkdir(parents=True)
        (cache / "vms").mkdir()
        (cache / "runs").mkdir()
        (cache / "images" / "abc.meta.json").write_text(
            json.dumps({"url": "https://example.com/x.qcow2", "size_bytes": 50 * 1024 * 1024})
        )
        r = runner.invoke(main, ["cache-list", "--cache-dir", str(cache)])
        assert r.exit_code == 0
        assert "example.com" in r.output
        assert "50" in r.output


class TestCacheClear:
    def test_refuses_without_confirmation(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        r = runner.invoke(main, ["cache-clear", "--cache-dir", str(tmp_path)], input="n\n")
        assert r.exit_code != 0 or "Aborted" in r.output

    def test_clears_with_yes_flag(self, runner: CliRunner, tmp_path: Path) -> None:
        cache = tmp_path / "c"
        (cache / "images").mkdir(parents=True)
        (cache / "vms" / "abc").mkdir(parents=True)
        (cache / "vms" / "abc" / "disk.qcow2").write_bytes(b"x")
        (cache / "runs").mkdir()

        r = runner.invoke(
            main, ["cache-clear", "--cache-dir", str(cache), "--yes"]
        )
        assert r.exit_code == 0
        # The vms subdir is recreated empty after clearing
        assert (cache / "vms").exists()
        assert not (cache / "vms" / "abc").exists()


class TestOrchestratorOption:
    """``--orchestrator URL`` on run/repl lets the person running the
    suite retarget a backend without touching the test code."""

    _FACTORY_WITH_REAL_ORCH = (
        "from testrange import (\n"
        "    Credential, Orchestrator, Test, VirtualNetwork, VM,\n"
        ")\n"
        "\n"
        "def _noop(orch):\n"
        "    pass\n"
        "\n"
        "def gen_tests():\n"
        "    return [Test(\n"
        "        Orchestrator(\n"
        "            networks=[VirtualNetwork('N', '10.0.0.0/24')],\n"
        "            vms=[VM(\n"
        "                name='x',\n"
        "                iso='https://example.com/debian.qcow2',\n"
        "                users=[Credential('root', 'pw')],\n"
        "            )],\n"
        "        ),\n"
        "        _noop,\n"
        "    )]\n"
    )

    def test_help_documents_the_url_form(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["run", "--help"])
        assert r.exit_code == 0
        assert "--orchestrator" in r.output
        # URL examples appear inline in the help text.
        assert "qemu:///system" in r.output
        assert "proxmox://" in r.output

    def test_unknown_scheme_rejected(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """vmware:// isn't implemented — the URL parser should refuse
        it with a clear message."""
        f = tmp_path / "case.py"
        f.write_text(self._FACTORY_WITH_REAL_ORCH)
        r = runner.invoke(
            main,
            ["run", f"{f}:gen_tests", "--orchestrator", "vmware://host"],
        )
        assert r.exit_code != 0
        assert "unknown orchestrator scheme" in r.output.lower()

    def test_proxmox_url_requires_auth(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """``proxmox://host`` without userinfo or ?token= is rejected —
        Proxmox needs creds before we spin anything up."""
        f = tmp_path / "case.py"
        f.write_text(self._FACTORY_WITH_REAL_ORCH)
        r = runner.invoke(
            main,
            ["run", f"{f}:gen_tests", "--orchestrator", "proxmox://pve.example.com"],
        )
        assert r.exit_code != 0
        assert "token" in r.output.lower() or "credentials" in r.output.lower()

    def test_proxmox_url_needs_host(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        f = tmp_path / "case.py"
        f.write_text(self._FACTORY_WITH_REAL_ORCH)
        r = runner.invoke(
            main,
            ["run", f"{f}:gen_tests", "--orchestrator", "proxmox://"],
        )
        assert r.exit_code != 0


class TestParseOrchestratorUrl:
    """Direct unit tests for the URL → backend-kwargs parser.

    Faster than going through Click for each variant.
    """

    def test_libvirt_uri_passes_through(self) -> None:
        from testrange._cli import _parse_orchestrator_url

        spec = _parse_orchestrator_url("qemu+ssh://alice@vmhost/system")
        assert spec["backend"] == "libvirt"
        assert spec["host"] == "qemu+ssh://alice@vmhost/system"

    def test_libvirt_scheme_rewrites_to_qemu_ssh(self) -> None:
        """``libvirt://alice@vmhost`` is a convenience alias that
        reshapes into the equivalent libvirt URI."""
        from testrange._cli import _parse_orchestrator_url

        spec = _parse_orchestrator_url("libvirt://alice@vmhost")
        assert spec["backend"] == "libvirt"
        assert spec["host"] == "qemu+ssh://alice@vmhost/system"

    def test_proxmox_user_password(self) -> None:
        from testrange._cli import _parse_orchestrator_url

        spec = _parse_orchestrator_url(
            "proxmox://root:hunter2@pve.example.com"
        )
        assert spec["backend"] == "proxmox"
        assert spec["host"] == "pve.example.com"
        assert spec["user"] == "root"
        assert spec["password"] == "hunter2"
        assert spec["token"] is None

    def test_proxmox_token_in_userinfo(self) -> None:
        from testrange._cli import _parse_orchestrator_url

        spec = _parse_orchestrator_url(
            "proxmox://abcdefghij@pve.example.com/pve01"
        )
        assert spec["token"] == "abcdefghij"
        assert spec["user"] is None
        assert spec["password"] is None
        assert spec["node"] == "pve01"

    def test_proxmox_token_query_param(self) -> None:
        """?token= takes precedence over userinfo (lets callers pass
        the full ``user@realm!name=secret`` blob without URL-encoding)."""
        from testrange._cli import _parse_orchestrator_url

        spec = _parse_orchestrator_url(
            "proxmox://pve.example.com?token=root!auto&storage=local-lvm"
        )
        assert spec["token"] == "root!auto"
        assert spec["storage"] == "local-lvm"
