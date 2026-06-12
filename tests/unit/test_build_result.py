"""Build-result protocol parsing (ADR §21).

``parse_build_result`` is the backend-independent reader for the framed
``TESTRANGE-RESULT:`` record a builder emits to the guest serial console. It
must short-circuit on a complete record, tolerate interleaved boot chatter,
survive binary log payloads (via base64), and wait for an incomplete record
without misfiring. The ``wait_for_build_result`` orchestration paths are
exercised end-to-end against the mock in ``test_build_phase.py``.
"""

from __future__ import annotations

import base64

from testrange.orchestrator.vm_build import BuildResult, parse_build_result


def _fail_bytes(rc: int, cmd: str, log: bytes) -> bytes:
    """Frame a fail record exactly as the builder's serial output would."""
    return (
        f'TESTRANGE-RESULT: fail rc={rc} cmd="{cmd}"\n'.encode()
        + b"TESTRANGE-LOG-BEGIN\n"
        + base64.b64encode(log)
        + b"\n"
        + b"TESTRANGE-LOG-END\n"
    )


class TestParseSuccess:
    def test_bare_ok(self) -> None:
        assert parse_build_result(b"TESTRANGE-RESULT: ok\n") == BuildResult(ok=True)

    def test_ok_after_boot_chatter(self) -> None:
        data = b"[    1.23] random kernel chatter\nTESTRANGE-RESULT: ok\n"
        assert parse_build_result(data) == BuildResult(ok=True)

    def test_ok_with_trailing_chatter(self) -> None:
        # The record may be followed by a final newline / poweroff noise.
        data = b"TESTRANGE-RESULT: ok\n[  shutting down ]\n"
        assert parse_build_result(data) == BuildResult(ok=True)


class TestParseIncomplete:
    def test_no_marker_yet(self) -> None:
        assert parse_build_result(b"booting, please wait...\n") is None

    def test_result_line_not_terminated(self) -> None:
        # Marker present but the line hasn't finished arriving — keep reading.
        assert parse_build_result(b"TESTRANGE-RESULT: o") is None

    def test_fail_with_unfinished_log_waits(self) -> None:
        # The fail line is whole but the framed log hasn't closed: not terminal
        # yet (the orchestrator keeps tailing for the rest of the log).
        partial = b'TESTRANGE-RESULT: fail rc=1 cmd="x"\nTESTRANGE-LOG-BEGIN\nQUJD'
        assert parse_build_result(partial) is None

    def test_fail_line_without_log_block_waits(self) -> None:
        whole_line = b'TESTRANGE-RESULT: fail rc=1 cmd="x"\n'
        assert parse_build_result(whole_line) is None


class TestParseFailure:
    def test_full_fail_record(self) -> None:
        data = _fail_bytes(100, "apt-get update", b"E: Could not get lock\n")
        result = parse_build_result(data)
        assert result is not None
        assert result.ok is False
        assert result.rc == 100
        assert result.cmd == "apt-get update"
        assert result.log == b"E: Could not get lock\n"

    def test_binary_log_survives_base64(self) -> None:
        payload = bytes(range(256))  # every byte value, incl. NUL and high bytes
        result = parse_build_result(_fail_bytes(2, "dd", payload))
        assert result is not None
        assert result.log == payload

    def test_fail_record_after_chatter(self) -> None:
        data = b"some log lines\nmore lines\n" + _fail_bytes(7, "false", b"boom")
        result = parse_build_result(data)
        assert result is not None and result.rc == 7 and result.cmd == "false"


class TestParseFinalPass:
    """``final=True`` is the end-of-stream (console closed) pass."""

    def test_fail_without_log_is_still_returned(self) -> None:
        # Guest announced failure then died before emitting the log block.
        data = b'TESTRANGE-RESULT: fail rc=5 cmd="modprobe"\n'
        assert parse_build_result(data, final=True) == BuildResult(
            ok=False, rc=5, cmd="modprobe", log=b""
        )

    def test_no_record_at_eof_is_none(self) -> None:
        # No marker at all: caller treats this as "powered off without a token".
        assert parse_build_result(b"kernel panic\n", final=True) is None

    def test_ok_without_trailing_newline_at_eof(self) -> None:
        assert parse_build_result(b"TESTRANGE-RESULT: ok", final=True) == BuildResult(ok=True)


class TestParseTokenStrictness:
    """Success is the explicit ``ok`` token, not any ``ok``-prefixed word."""

    def test_ok_prefixed_token_is_not_success(self) -> None:
        # "okay" / "ok_pending" must NOT parse as success — they are unrecognized
        # tokens on a complete line, so they fail rather than green a bad build.
        # The raw line is captured in the log for triage (REL-29).
        for tok in (b"okay", b"ok_pending"):
            result = parse_build_result(b"TESTRANGE-RESULT: " + tok + b"\n")
            assert result is not None and result.ok is False
            assert tok.decode() in result.log.decode()

    def test_fail_prefixed_token_is_not_a_fail_record(self) -> None:
        # Only the bare ``fail`` token triggers the rc/cmd/log fail path; an
        # unrecognized ``fail``-prefixed token is a generic failure that still
        # carries the offending line for triage.
        result = parse_build_result(b"TESTRANGE-RESULT: failure\n")
        assert result is not None and result.ok is False
        assert "failure" in result.log.decode()

    def test_ok_with_trailing_field_still_succeeds(self) -> None:
        # First-token match: trailing chatter on the same line doesn't break ok.
        assert parse_build_result(b"TESTRANGE-RESULT: ok extra\n") == BuildResult(ok=True)


class TestParseMultipleMarkers:
    """Boot chatter can print the literal marker before the real record; an
    earlier broken/unrecognized marker must not mask a later complete one
    (REL-29 — otherwise the build hangs to the watchdog)."""

    def test_chatter_marker_then_real_ok(self) -> None:
        # A complete unrecognized line, then the real ok further down.
        data = b"TESTRANGE-RESULT: starting provisioning\nTESTRANGE-RESULT: ok\n"
        assert parse_build_result(data) == BuildResult(ok=True)

    def test_broken_fail_frame_then_real_ok(self) -> None:
        # An earlier fail whose log frame never closed (chatter echoing the
        # script), then the genuine ok — the unfinished frame must be skipped.
        data = (
            b'TESTRANGE-RESULT: fail rc=1 cmd="echo"\nTESTRANGE-LOG-BEGIN\nTESTRANGE-RESULT: ok\n'
        )
        assert parse_build_result(data) == BuildResult(ok=True)

    def test_chatter_marker_then_real_fail(self) -> None:
        data = b"TESTRANGE-RESULT: noise\n" + _fail_bytes(9, "make", b"boom")
        result = parse_build_result(data)
        assert result is not None and result.ok is False and result.rc == 9
        assert result.cmd == "make" and result.log == b"boom"

    def test_unrecognized_then_incomplete_tail_waits(self) -> None:
        # Chatter marker (complete) followed by a real marker still mid-line:
        # not actionable yet — keep reading rather than failing on the chatter.
        data = b"TESTRANGE-RESULT: noise\nTESTRANGE-RESULT: o"
        assert parse_build_result(data) is None
