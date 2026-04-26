"""Unit tests for :mod:`testrange._cli`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

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


class TestCleanupCommand:
    """Smoke tests for ``testrange cleanup MODULE[:FACTORY] RUN_ID``.

    The deep semantics of cleanup are exercised in test_cleanup.py;
    these just confirm the CLI wiring (target parsing, run_id
    pass-through, exit codes) is right."""

    _RUN_ID = "00000000-0000-0000-0000-000000000000"

    def _empty_factory(self, tmp_path: Path) -> Path:
        # An empty list of tests is a valid cleanup target — the
        # cleanup CLI prints "no tests" and exits 0.  The next test
        # exercises the path with an actual test in the list.
        f = tmp_path / "bare.py"
        f.write_text("def gen_tests():\n    return []\n")
        return f

    def test_no_tests_exits_zero(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        f = self._empty_factory(tmp_path)
        r = runner.invoke(main, ["cleanup", str(f), self._RUN_ID])
        assert r.exit_code == 0

    def test_invokes_orchestrator_cleanup(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Loads the factory, finds the orchestrator, calls cleanup
        with the supplied run id."""
        f = tmp_path / "case.py"
        f.write_text(
            "from testrange import (\n"
            "    Test, Orchestrator, VM, Credential, vCPU, Memory, vNIC,\n"
            ")\n"
            "def gen_tests():\n"
            "    return [\n"
            "        Test(\n"
            "            Orchestrator(vms=[\n"
            "                VM(name='web', iso='https://x/y.qcow2',\n"
            "                   users=[Credential('root', 'pw')],\n"
            "                   devices=[vCPU(1), Memory(1)]),\n"
            "            ]),\n"
            "            lambda o: None,\n"
            "        ),\n"
            "    ]\n"
        )

        called: list[str] = []

        def _fake_cleanup(self, run_id: str) -> None:  # noqa: ARG001
            called.append(run_id)

        from testrange.backends.libvirt.orchestrator import Orchestrator
        monkeypatch.setattr(Orchestrator, "cleanup", _fake_cleanup)

        r = runner.invoke(main, ["cleanup", str(f), self._RUN_ID])

        assert r.exit_code == 0, r.output
        assert called == [self._RUN_ID]
        assert "cleanup ok" in r.output

    def test_failure_exits_one(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup raising bumps the exit code to 1 so scripts can
        fail loudly on partial sweeps."""
        f = tmp_path / "case.py"
        f.write_text(
            "from testrange import (\n"
            "    Test, Orchestrator, VM, Credential,\n"
            ")\n"
            "def gen_tests():\n"
            "    return [Test(Orchestrator(vms=[VM('a', 'b', \n"
            "        [Credential('r','p')])]), lambda o: None)]\n"
        )

        from testrange.backends.libvirt.orchestrator import Orchestrator
        monkeypatch.setattr(
            Orchestrator,
            "cleanup",
            lambda self, run_id: (_ for _ in ()).throw(  # noqa: ARG005
                RuntimeError("simulated leftover"),
            ),
        )

        r = runner.invoke(main, ["cleanup", str(f), self._RUN_ID])

        assert r.exit_code == 1
        assert "simulated leftover" in r.output


class TestRunCommand:
    def test_bare_target_defaults_to_gen_tests(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # No ``:factory`` suffix — should look up ``gen_tests``.
        f = tmp_path / "bare.py"
        f.write_text("def gen_tests():\n    return []\n")
        r = runner.invoke(main, ["run", str(f)])
        # Empty list is a valid result; run succeeds (exit 0).
        assert r.exit_code == 0

    def test_bare_target_missing_gen_tests_factory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Default factory resolves but isn't defined in the module.
        f = tmp_path / "noops.py"
        f.write_text("# no gen_tests defined\n")
        r = runner.invoke(main, ["run", str(f)])
        assert r.exit_code == 1
        assert "gen_tests" in r.output

    def test_empty_factory_name(self, runner: CliRunner) -> None:
        # Trailing colon with no factory is still an error — we don't
        # want typos ("run x:") to silently fall back to gen_tests.
        r = runner.invoke(main, ["run", "mymodule:"])
        assert r.exit_code == 2
        assert "empty factory" in r.output

    def test_empty_module_part(self, runner: CliRunner) -> None:
        r = runner.invoke(main, ["run", ":gen_tests"])
        assert r.exit_code == 2
        assert "module[:factory]" in r.output

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
        """vmware:// isn't implemented — no backend claims it, so the
        CLI must refuse with a clear message rather than silently
        constructing something default."""
        f = tmp_path / "case.py"
        f.write_text(self._FACTORY_WITH_REAL_ORCH)
        r = runner.invoke(
            main,
            ["run", f"{f}:gen_tests", "--orchestrator", "vmware://host"],
        )
        assert r.exit_code != 0
        assert "no backend claims" in r.output.lower()

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


class TestBackendUrlDispatch:
    """Each backend's ``cli_build_orchestrator`` claims its own URL
    shapes and returns a constructed orchestrator, or ``None`` for a
    URL it doesn't recognise.  The CLI dispatcher iterates over the
    registered backends — no scheme knowledge in the CLI itself.
    """

    def _fake_original(self, tmp_path: Path | None = None) -> MagicMock:
        """Return a MagicMock orchestrator shaped like the libvirt one
        (exposes ``_networks`` / ``_vm_list`` / ``_cache.root``).  The
        backend ``cli_build_orchestrator`` functions reuse those
        attributes to reconstruct the orchestrator under a new URL."""
        m = MagicMock()
        m._networks = []
        m._vm_list = []
        # CacheManager normalises ``root`` through Path; give it a real
        # tmp path so construction succeeds.
        m._cache.root = tmp_path or Path("/tmp")
        return m

    def test_libvirt_uri_builds_libvirt_orchestrator(
        self, tmp_path: Path
    ) -> None:
        from testrange.backends.libvirt import (
            Orchestrator,
            cli_build_orchestrator,
        )
        orch = cli_build_orchestrator(
            "qemu+ssh://alice@vmhost/system",
            self._fake_original(tmp_path),
        )
        assert isinstance(orch, Orchestrator)
        assert orch._host == "qemu+ssh://alice@vmhost/system"

    def test_libvirt_scheme_rewrites_to_qemu_ssh(
        self, tmp_path: Path
    ) -> None:
        """``libvirt://alice@vmhost`` is a convenience alias that
        reshapes into the equivalent libvirt URI."""
        from testrange.backends.libvirt import cli_build_orchestrator

        orch = cli_build_orchestrator(
            "libvirt://alice@vmhost", self._fake_original(tmp_path),
        )
        assert orch is not None
        assert orch._host == "qemu+ssh://alice@vmhost/system"

    def test_libvirt_returns_none_for_proxmox_url(self) -> None:
        """A backend must decline URLs that aren't its — letting the
        dispatcher try the next backend."""
        from testrange.backends.libvirt import cli_build_orchestrator
        assert cli_build_orchestrator(
            "proxmox://root:pw@pve.example.com", self._fake_original(),
        ) is None

    def test_proxmox_returns_none_for_libvirt_url(self) -> None:
        from testrange.backends.proxmox import cli_build_orchestrator
        assert cli_build_orchestrator(
            "qemu+ssh://vm/system", self._fake_original(),
        ) is None

    def test_proxmox_user_password(self, tmp_path: Path) -> None:
        from testrange.backends.proxmox import (
            ProxmoxOrchestrator,
            cli_build_orchestrator,
        )
        orch = cli_build_orchestrator(
            "proxmox://root:hunter2@pve.example.com",
            self._fake_original(tmp_path),
        )
        assert isinstance(orch, ProxmoxOrchestrator)
        assert orch._host == "pve.example.com"
        # CLI lifts ``user:password`` out of the URL into the
        # orchestrator's explicit auth kwargs.
        assert orch._user == "root"
        assert orch._password == "hunter2"

    def test_proxmox_token_in_userinfo(self, tmp_path: Path) -> None:
        from testrange.backends.proxmox import cli_build_orchestrator
        orch = cli_build_orchestrator(
            "proxmox://abcdefghij@pve.example.com/pve01",
            self._fake_original(tmp_path),
        )
        assert orch is not None
        # Userinfo without a colon is treated as a token.  The
        # orchestrator stashes it on ``_legacy_token`` until the URL
        # handler grows explicit ``token_name``/``token_value``
        # parsing for the ``user@realm!name=secret`` form.
        assert orch._legacy_token == "abcdefghij"
        assert orch._node == "pve01"

    def test_proxmox_token_query_param(self, tmp_path: Path) -> None:
        """``?token=`` takes precedence over userinfo (lets callers
        pass the full ``user@realm!name=secret`` blob without
        URL-encoding)."""
        from testrange.backends.proxmox import (
            ProxmoxOrchestrator,
            cli_build_orchestrator,
        )
        orch = cli_build_orchestrator(
            "proxmox://pve.example.com?token=root!auto&storage=local-lvm",
            self._fake_original(tmp_path),
        )
        assert isinstance(orch, ProxmoxOrchestrator)
        assert orch._legacy_token == "root!auto"
        assert orch._storage == "local-lvm"

    def test_central_dispatcher_iterates_backends(
        self, tmp_path: Path
    ) -> None:
        from testrange.backends import cli_build_orchestrator
        from testrange.backends.libvirt import Orchestrator

        # libvirt URL — first backend in the registry that claims it.
        orch = cli_build_orchestrator(
            "qemu:///system", self._fake_original(tmp_path),
        )
        assert isinstance(orch, Orchestrator)

    def test_central_dispatcher_returns_none_on_unknown(
        self, tmp_path: Path
    ) -> None:
        from testrange.backends import cli_build_orchestrator
        assert cli_build_orchestrator(
            "madeup://nothing", self._fake_original(tmp_path),
        ) is None


# ---------------------------------------------------------------------------
# describe command — verifies the pretty-printer's output for plain and
# Hypervisor (nested) VMs.  Written as a tmp-path module file + CliRunner
# roundtrip so it exercises the real MODULE:FACTORY loader path.
# ---------------------------------------------------------------------------


_PLAIN_DESCRIBE_MODULE = '''
from testrange import (
    VM, Credential, HardDrive, Memory, Orchestrator,
    Test, VirtualNetwork, vNIC, vCPU,
)

def gen_tests():
    return [Test(
        Orchestrator(
            networks=[
                VirtualNetwork("Net", "10.0.0.0/24", internet=True, dhcp=True),
            ],
            vms=[
                VM(
                    name="web",
                    iso="https://example.com/debian.qcow2",
                    users=[Credential("root", "pw")],
                    devices=[
                        vCPU(1), Memory(1), HardDrive(10),
                        vNIC("Net", ip="10.0.0.5"),
                    ],
                ),
            ],
        ),
        lambda orch: None,
        name="plain-smoke",
    )]
'''


_NESTED_DESCRIBE_MODULE = '''
from testrange import (
    VM, Credential, HardDrive, Hypervisor, LibvirtOrchestrator, Memory,
    Orchestrator, Test, VirtualNetwork, vNIC, vCPU,
)

def gen_tests():
    root = Credential("root", "pw", ssh_key="ssh-ed25519 AAA")
    return [Test(
        Orchestrator(
            networks=[
                VirtualNetwork("OuterNet", "10.0.0.0/24", internet=True),
            ],
            vms=[
                VM(
                    name="sidecar",
                    iso="https://example.com/debian.qcow2",
                    users=[root],
                    devices=[
                        vCPU(1), Memory(1), HardDrive(10),
                        vNIC("OuterNet", ip="10.0.0.11"),
                    ],
                ),
                Hypervisor(
                    name="hv",
                    iso="https://example.com/debian.qcow2",
                    users=[root],
                    communicator="ssh",
                    devices=[
                        vCPU(2), Memory(4), HardDrive(40),
                        vNIC("OuterNet", ip="10.0.0.10"),
                    ],
                    orchestrator=LibvirtOrchestrator,
                    networks=[
                        VirtualNetwork("PublicNet", "10.42.0.0/24", internet=True),
                        VirtualNetwork("PrivateNet", "10.43.0.0/24", internet=False),
                    ],
                    vms=[
                        VM(
                            name="webpublic",
                            iso="https://example.com/debian.qcow2",
                            users=[root],
                            devices=[
                                vCPU(1), Memory(1), HardDrive(10),
                                vNIC("PublicNet", ip="10.42.0.5"),
                            ],
                        ),
                    ],
                ),
            ],
        ),
        lambda orch: None,
        name="nested-smoke",
    )]
'''


def _write_module(tmp_path: Path, source: str) -> str:
    mod = tmp_path / "descmod.py"
    mod.write_text(source)
    return f"{mod}:gen_tests"


class TestDescribeCommand:
    def test_plain_vm_renders(self, runner: CliRunner, tmp_path: Path) -> None:
        target = _write_module(tmp_path, _PLAIN_DESCRIBE_MODULE)
        # color_off keeps ANSI escapes out of the output so substring
        # matching against network names stays robust.
        r = runner.invoke(main, ["describe", target], color=False)
        assert r.exit_code == 0, r.output
        assert "Test: plain-smoke" in r.output
        assert "Networks (1)" in r.output
        assert "VMs (1)" in r.output
        assert "web" in r.output
        # Plain VMs never emit the Hypervisor tag or inner sections.
        assert "Hypervisor" not in r.output
        assert "Inner networks" not in r.output
        assert "Inner VMs" not in r.output

    def test_nested_shows_hypervisor_tag(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        target = _write_module(tmp_path, _NESTED_DESCRIBE_MODULE)
        r = runner.invoke(main, ["describe", target], color=False)
        assert r.exit_code == 0, r.output
        # The hypervisor's header line carries the driver annotation so
        # readers can see the topology is nested without scrolling.
        assert "Hypervisor → Orchestrator" in r.output

    def test_nested_inner_blocks_present(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        target = _write_module(tmp_path, _NESTED_DESCRIBE_MODULE)
        r = runner.invoke(main, ["describe", target], color=False)
        assert r.exit_code == 0, r.output
        assert "Inner networks (2)" in r.output
        assert "Inner VMs (1)" in r.output
        assert "PublicNet" in r.output
        assert "PrivateNet" in r.output
        assert "webpublic" in r.output

    def test_nested_inner_nic_resolves_against_inner_networks(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """An inner VM's vNIC must be interpreted against
        the *inner* networks list, not the outer — otherwise a nic ref
        like 'PublicNet' would show as 'auto-reserved' instead of
        static."""
        target = _write_module(tmp_path, _NESTED_DESCRIBE_MODULE)
        r = runner.invoke(main, ["describe", target], color=False)
        assert r.exit_code == 0, r.output
        assert "static 10.42.0.5" in r.output

    def test_sibling_vm_before_hypervisor(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Regression guard: sidecar must render before hv without the
        inner block bleeding into the sidecar's section."""
        target = _write_module(tmp_path, _NESTED_DESCRIBE_MODULE)
        r = runner.invoke(main, ["describe", target], color=False)
        lines = r.output.splitlines()
        # Find sidecar and hv headers; sidecar's line index is lower.
        sidecar_idx = next(i for i, line in enumerate(lines) if "sidecar" in line)
        hv_idx = next(i for i, line in enumerate(lines) if " hv " in line or line.endswith(" hv"))
        inner_idx = next(
            i for i, line in enumerate(lines) if "Inner networks" in line
        )
        assert sidecar_idx < hv_idx < inner_idx
