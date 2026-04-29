"""Unit tests for :func:`testrange._debug.pause_on_error_if_enabled`."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest

from testrange._debug import pause_on_error_if_enabled


class TestPauseOnErrorIfEnabled:
    def test_no_op_without_env_var(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The default — no env var — must not block on input(), must
        # not write anything.  Tests run unsupervised in CI; even one
        # blocking input() would hang the suite.
        monkeypatch.delenv("TESTRANGE_PAUSE_ON_ERROR", raising=False)
        # If this somehow tried to read input the test would hang;
        # the assertion below is a quick sanity check on output.
        pause_on_error_if_enabled("anything", orchestrator=None)
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""

    def test_pauses_on_input_when_env_var_set(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        # Patch input() to simulate the operator pressing Enter.
        # ``input()`` with no stdin would EOFError; we make it
        # explicit so the test is deterministic.
        called_with: list[str] = []
        def _fake_input(prompt: str = "") -> str:
            called_with.append(prompt)
            return ""
        monkeypatch.setattr("builtins.input", _fake_input)

        pause_on_error_if_enabled("setup phase failed", orchestrator=None)

        out = capsys.readouterr()
        assert "TESTRANGE_PAUSE_ON_ERROR" in out.err
        assert "setup phase failed" in out.err
        assert len(called_with) == 1
        assert "tear down" in called_with[0]

    def test_eof_lets_teardown_proceed(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # EOFError (e.g. running with no tty) MUST not propagate —
        # the operator is signalling "I'm done, tear down".  Same
        # story for KeyboardInterrupt (Ctrl+C at the prompt).
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        def _eof(*_a: object, **_kw: object) -> str:
            raise EOFError()
        monkeypatch.setattr("builtins.input", _eof)
        pause_on_error_if_enabled("anything")  # must not raise

        out = capsys.readouterr()
        assert "interrupted" in out.err

    def test_keyboard_interrupt_lets_teardown_proceed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        def _kbi(*_a: object, **_kw: object) -> str:
            raise KeyboardInterrupt()
        monkeypatch.setattr("builtins.input", _kbi)
        pause_on_error_if_enabled("anything")  # must not raise

    def test_orchestrator_keep_alive_hints_printed(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # When an orchestrator is passed, its ``keep_alive_hints()``
        # output is included so the operator can see exactly which
        # resources are still up — virsh/pvesh invocations the user
        # would otherwise have to derive from the run-id-suffixed
        # names by hand.
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

        orch = MagicMock()
        orch.keep_alive_hints.return_value = [
            "virsh destroy tr-build-proxmox-98de8ecd",
            "virsh net-destroy tr-instal-98de",
        ]
        pause_on_error_if_enabled("test", orchestrator=orch)

        out = capsys.readouterr()
        assert "tr-build-proxmox-98de8ecd" in out.err
        assert "tr-instal-98de" in out.err

    def test_keep_alive_hints_failure_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``keep_alive_hints`` raising would be a poor reason to
        # skip the prompt — the whole point of the pause is debug
        # access, and a hint generation failure shouldn't deny it.
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

        orch = MagicMock()
        orch.keep_alive_hints.side_effect = RuntimeError("boom")
        pause_on_error_if_enabled("test", orchestrator=orch)  # no raise

    def test_prints_active_exception_traceback(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When called from inside an ``except`` handler, the prompt
        prints the triggering exception's traceback first — operators
        shouldn't have to grep above the prompt to find what failed.
        """
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

        try:
            raise ValueError("the specific failure operators care about")
        except ValueError:
            pause_on_error_if_enabled("setup phase failed")

        out = capsys.readouterr().err
        assert "Exception that triggered the pause" in out
        assert "ValueError: the specific failure operators care about" in out
        # Traceback frame info ("File ..., line ...") is in the format
        # output too — sanity-check that we got real format_exception
        # output rather than just a stringified exception.
        assert 'File "' in out

    def test_no_traceback_when_called_outside_except(
        self, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Outside an ``except`` handler ``sys.exc_info`` is empty;
        # the prompt must skip the traceback section cleanly rather
        # than print "None".
        monkeypatch.setenv("TESTRANGE_PAUSE_ON_ERROR", "1")
        monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")
        pause_on_error_if_enabled("just a debug stop")
        out = capsys.readouterr().err
        assert "Exception that triggered the pause" not in out
        assert "None" not in out
