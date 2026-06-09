"""Tests for the shared rich Consoles (ADR-0029)."""

from __future__ import annotations

from testrange._console import err_console, out_console


def test_out_console_is_a_shared_singleton() -> None:
    assert out_console() is out_console()


def test_err_console_is_a_shared_singleton() -> None:
    assert err_console() is err_console()


def test_out_is_stdout_err_is_stderr() -> None:
    # The split is the whole point: data on stdout, diagnostics on stderr.
    assert out_console().stderr is False
    assert err_console().stderr is True
    assert out_console() is not err_console()


def test_capture_round_trips_rendered_output() -> None:
    console = out_console()
    with console.capture() as cap:
        console.print("hello range")
    assert "hello range" in cap.get()
