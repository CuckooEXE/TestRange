"""Tests for the terminal-control scrubber (CORE-6 prerequisite)."""

from __future__ import annotations

import pytest

from testrange._ansi import scrub_terminal_control


def test_plain_text_unchanged() -> None:
    assert scrub_terminal_control("hello world") == "hello world"


def test_newlines_and_tabs_preserved() -> None:
    assert scrub_terminal_control("a\tb\nc\n") == "a\tb\nc\n"


def test_csi_colour_codes_stripped() -> None:
    assert scrub_terminal_control("\x1b[31mred\x1b[0m text") == "red text"


def test_csi_cursor_and_erase_stripped() -> None:
    # clear-screen + home + erase-line — the live-run terminal-hijack culprits.
    assert scrub_terminal_control("\x1b[2J\x1b[H\x1b[Kclean") == "clean"


def test_cursor_position_report_response_stripped() -> None:
    # A guest answering an ESC[6n query emits ESC[<row>;<col>R into the stream.
    assert scrub_terminal_control("before\x1b[12;40Rafter") == "beforeafter"


def test_embedded_carriage_returns_stripped() -> None:
    # \r drives the overwrite; concatenation of the segments is acceptable.
    assert scrub_terminal_control("loading...\rdone") == "loading...done"


def test_osc_title_sequence_stripped_bel_terminated() -> None:
    assert scrub_terminal_control("\x1b]0;window title\x07payload") == "payload"


def test_osc_title_sequence_stripped_st_terminated() -> None:
    assert scrub_terminal_control("\x1b]0;title\x1b\\payload") == "payload"


def test_c0_control_bytes_stripped() -> None:
    # BEL, backspace, vertical tab, form feed, DEL — all dropped.
    assert scrub_terminal_control("a\x07b\x08c\x0bd\x0ce\x7ff") == "abcdef"


def test_fe_escape_stripped() -> None:
    # ESC c (full reset) and a charset-select sequence.
    assert scrub_terminal_control("\x1bcx\x1b(By") == "xy"


@pytest.mark.parametrize(
    "noisy",
    [
        "\x1b[1;32m[  OK  ]\x1b[0m Started thing.\r\n",
        "\x1b[2K\rProgress: 100%\x1b[0m\n",
    ],
)
def test_realistic_boot_chatter_reduces_to_printable(noisy: str) -> None:
    out = scrub_terminal_control(noisy)
    assert "\x1b" not in out
    assert "\r" not in out
    # Newline structure is preserved.
    assert out.endswith("\n")
