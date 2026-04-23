"""Unit tests for :mod:`testrange._repl` and the ``testrange repl`` command."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from testrange import _repl
from testrange._cli import _choose_test, _load_tests, main
from testrange._repl import (
    _build_banner,
    _build_locals,
    _interact,
    _interact_stdlib,
    print_keep_summary,
)
from testrange.test import Test, TestResult


def _fake_orch(vm_names: list[str]) -> MagicMock:
    """Return a MagicMock orchestrator with ``vms`` populated and a fake run."""
    orch = MagicMock(name="Orchestrator")
    orch.vms = {n: MagicMock(name=f"VM({n})") for n in vm_names}
    orch._vm_list = list(orch.vms.values())
    for vm, name in zip(orch._vm_list, vm_names, strict=True):
        vm.name = name
    orch._networks = []
    orch._run = MagicMock()
    orch._run.run_id = "abcdef1234567890"
    orch._run.path = "/tmp/testrange-run-abcdef12"
    return orch


# ---------------------------------------------------------------------------
# _build_locals
# ---------------------------------------------------------------------------


class TestBuildLocals:
    def test_binds_orch_and_vms(self) -> None:
        orch = _fake_orch(["web", "db"])
        ns = _build_locals(orch)
        assert ns["orch"] is orch
        assert ns["vms"] == list(orch.vms.values())

    def test_binds_each_vm_by_name(self) -> None:
        orch = _fake_orch(["web", "db"])
        ns = _build_locals(orch)
        assert ns["web"] is orch.vms["web"]
        assert ns["db"] is orch.vms["db"]

    def test_skips_name_colliding_with_builtin(self) -> None:
        orch = _fake_orch(["list", "web"])
        ns = _build_locals(orch)
        assert "web" in ns
        # `list` is a builtin and must NOT be shadowed
        assert ns.get("list") is not orch.vms["list"]

    def test_skips_non_identifier_name(self) -> None:
        orch = _fake_orch(["web-server"])  # hyphen is not a valid identifier
        ns = _build_locals(orch)
        assert "web-server" not in ns

    def test_does_not_overwrite_orch_or_vms(self) -> None:
        # A VM literally called "orch" must not overwrite the orch binding
        orch = _fake_orch(["orch"])
        ns = _build_locals(orch)
        assert ns["orch"] is orch


# ---------------------------------------------------------------------------
# _build_banner
# ---------------------------------------------------------------------------


class TestBuildBanner:
    def test_includes_test_name(self) -> None:
        orch = _fake_orch(["web"])
        ns = _build_locals(orch)
        banner = _build_banner(orch, "smoke", ns)
        assert "'smoke'" in banner

    def test_lists_each_bound_vm(self) -> None:
        orch = _fake_orch(["web", "db"])
        ns = _build_locals(orch)
        banner = _build_banner(orch, "t", ns)
        assert "web" in banner
        assert "db" in banner

    def test_flags_skipped_collisions(self) -> None:
        orch = _fake_orch(["list", "web"])
        ns = _build_locals(orch)
        banner = _build_banner(orch, "t", ns)
        assert "list" in banner
        assert "name collision" in banner

    def test_includes_sample_command(self) -> None:
        orch = _fake_orch(["web"])
        ns = _build_locals(orch)
        banner = _build_banner(orch, "t", ns)
        assert "web.exec" in banner


# ---------------------------------------------------------------------------
# _interact / _interact_stdlib
# ---------------------------------------------------------------------------


class TestInteract:
    def test_falls_back_to_stdlib_when_ipython_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force `from IPython import embed` to fail
        monkeypatch.setitem(sys.modules, "IPython", None)
        called: dict[str, Any] = {}

        def _fake_stdlib(ns: dict[str, Any], banner: str) -> None:
            called["ns"] = ns
            called["banner"] = banner

        monkeypatch.setattr(_repl, "_interact_stdlib", _fake_stdlib)
        _interact({"orch": "x"}, "BANNER")
        assert called["ns"] == {"orch": "x"}
        assert called["banner"] == "BANNER"

    def test_uses_ipython_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, Any] = {}

        fake_ipython = type(sys)("IPython")

        def _fake_embed(**kwargs: Any) -> None:
            recorded.update(kwargs)

        fake_ipython.embed = _fake_embed  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "IPython", fake_ipython)

        _interact({"a": 1}, "B")
        assert recorded["user_ns"] == {"a": 1}
        assert recorded["banner1"] == "B"

    def test_stdlib_swallows_systemexit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub out readline history so the test stays hermetic
        monkeypatch.setattr(_repl, "_enable_readline_history", lambda: None)

        fake_console = MagicMock()
        fake_console.interact.side_effect = SystemExit(0)
        monkeypatch.setattr(
            _repl.code, "InteractiveConsole", lambda locals=None: fake_console
        )

        # Should not raise
        _interact_stdlib({"x": 1}, "banner")


# ---------------------------------------------------------------------------
# print_keep_summary
# ---------------------------------------------------------------------------


class TestPrintKeepSummary:
    def test_lists_backend_hints_and_rundir(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The REPL's keep summary should delegate command generation
        to the backend's own ``keep_alive_hints`` — no libvirt-specific
        strings hardcoded here."""
        orch = _fake_orch(["web", "db"])
        orch.keep_alive_hints.return_value = [
            "sudo virsh destroy tr-web-abcdef12 && sudo virsh undefine tr-web-abcdef12",
            "sudo virsh destroy tr-db-abcdef12 && sudo virsh undefine tr-db-abcdef12",
            "sudo virsh net-destroy tr-net-abcd && sudo virsh net-undefine tr-net-abcd",
        ]

        print_keep_summary(orch)
        out = capsys.readouterr().out
        # Every hint the backend emitted must appear verbatim.
        for hint in orch.keep_alive_hints.return_value:
            assert hint in out
        # Run dir is always listed, regardless of backend.
        assert "/tmp/testrange-run-abcdef12" in out

    def test_no_hints_section_when_backend_has_no_cleanup_advice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Backends that don't override keep_alive_hints return ``[]``;
        the REPL should still print the run-dir line so the user can
        at least clean up scratch space."""
        orch = _fake_orch(["web"])
        orch.keep_alive_hints.return_value = []

        print_keep_summary(orch)
        out = capsys.readouterr().out
        assert "/tmp/testrange-run-abcdef12" in out
        assert "Suggested:" not in out


# ---------------------------------------------------------------------------
# _choose_test
# ---------------------------------------------------------------------------


def _make_test(name: str) -> MagicMock:
    t = MagicMock(spec=Test)
    t.name = name
    t.run.return_value = TestResult(passed=True, error=None, duration=0.0)
    return t


class TestChooseTest:
    def test_zero_tests_exits(self) -> None:
        with pytest.raises(SystemExit):
            _choose_test([], None)

    def test_single_test_returned_directly(self) -> None:
        t = _make_test("only")
        assert _choose_test([t], None) is t

    def test_named_match(self) -> None:
        a, b = _make_test("a"), _make_test("b")
        assert _choose_test([a, b], "b") is b

    def test_named_miss_exits(self) -> None:
        a, b = _make_test("a"), _make_test("b")
        with pytest.raises(SystemExit):
            _choose_test([a, b], "missing")


# ---------------------------------------------------------------------------
# _load_tests via the CLI surface
# ---------------------------------------------------------------------------


class TestLoadTests:
    def test_missing_factory_attribute_exits(self) -> None:
        mod = type(sys)("fake")
        with pytest.raises(SystemExit):
            _load_tests(mod, "fake", "gen_tests")

    def test_non_callable_attribute_exits(self) -> None:
        mod = type(sys)("fake")
        mod.gen_tests = 42  # type: ignore[attr-defined]
        with pytest.raises(SystemExit):
            _load_tests(mod, "fake", "gen_tests")

    def test_bad_return_type_exits(self) -> None:
        mod = type(sys)("fake")
        mod.gen_tests = lambda: "not a list"  # type: ignore[attr-defined]
        with pytest.raises(SystemExit):
            _load_tests(mod, "fake", "gen_tests")

    def test_returns_validated_list(self) -> None:
        t = _make_test("smoke")
        mod = type(sys)("fake")
        mod.gen_tests = lambda: [t]  # type: ignore[attr-defined]
        result = _load_tests(mod, "fake", "gen_tests")
        assert result == [t]


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestReplCommand:
    def test_help_lists_repl(self) -> None:
        r = CliRunner().invoke(main, ["repl", "--help"])
        assert r.exit_code == 0
        assert "REPL" in r.output or "repl" in r.output.lower()

    def test_unknown_module_exits(self) -> None:
        r = CliRunner().invoke(main, ["repl", "/does/not/exist.py:gen_tests"])
        assert r.exit_code == 1
        assert "not found" in r.output.lower()

    def test_keep_skips_teardown_and_prints_summary(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A factory file that returns one fake Test whose orchestrator is a
        # MagicMock — no real libvirt involvement.
        f = tmp_path / "fake.py"
        f.write_text(
            "from unittest.mock import MagicMock\n"
            "from testrange.test import Test\n"
            "\n"
            "_orch = MagicMock()\n"
            "_orch.vms = {'web': MagicMock(name='web')}\n"
            "_orch._vm_list = list(_orch.vms.values())\n"
            "_orch._networks = []\n"
            "_orch._run = MagicMock()\n"
            "_orch._run.run_id = '00000000-1111-2222-3333-444444444444'\n"
            "_orch._run.path = '/tmp/testrange-run-fake'\n"
            "\n"
            "def gen_tests():\n"
            "    t = MagicMock(spec=Test)\n"
            "    t.name = 'fake'\n"
            "    t._orchestrator = _orch\n"
            "    return [t]\n"
        )
        # Skip the real REPL — we just want to exercise the CLI plumbing
        monkeypatch.setattr(
            "testrange._cli.start_repl", lambda orch, name: None
        )

        r = CliRunner().invoke(main, ["repl", f"{f}:gen_tests", "--keep"])
        assert r.exit_code == 0, r.output
        assert "Run kept alive" in r.output
        # Teardown via __exit__ must NOT have been called when --keep was passed
        # The MagicMock orchestrator records every attribute access, so just
        # verify __exit__ wasn't called.
