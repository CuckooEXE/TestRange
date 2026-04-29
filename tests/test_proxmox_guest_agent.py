"""Tests for :class:`ProxmoxGuestAgentCommunicator`.

Drives every method against a mocked proxmoxer client whose
``nodes(node).qemu(vmid).agent(...)`` calls record what would have
been sent to PVE.  Covers the happy path, the hyphenated-endpoint
escape hatch (``agent("file-read")`` etc.), the chunking / size
guards on file IO, and the timeout behaviour of ``wait_ready`` /
``exec``.

No live PVE — that lives in ``tests/test_proxmox_live.py``.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from testrange.backends.proxmox.guest_agent import (
    ProxmoxGuestAgentCommunicator,
    _b64_payload_to_bytes,
    _text_payload_to_bytes,
)
from testrange.communication.base import ExecResult
from testrange.exceptions import GuestAgentError, VMTimeoutError


# =====================================================================
# Helpers
# =====================================================================


class _EndpointDict(dict):
    """Dict that lazily mints a MagicMock on first lookup so tests
    can both pre-populate (``endpoints['ping'].post.side_effect = ...``)
    AND let the code-under-test trigger missing keys lazily."""

    def __missing__(self, key: str) -> MagicMock:
        mock = MagicMock(name=f"agent({key!r})")
        self[key] = mock
        return mock


def _agent_call_recorder() -> tuple[MagicMock, _EndpointDict]:
    """Build a proxmoxer-shaped client whose ``agent("name")`` calls
    each return a fresh MagicMock so a test can assert on them
    individually.

    Returns (client, endpoints) where ``endpoints`` auto-creates
    entries on first lookup — both from test setup and from the
    code-under-test.  Re-uses the same MagicMock for repeated
    lookups of the same endpoint so call-count assertions are stable.
    """
    client = MagicMock()
    endpoints = _EndpointDict()
    agent = MagicMock()
    agent.side_effect = lambda name: endpoints[name]
    client.nodes.return_value.qemu.return_value.agent = agent
    return client, endpoints


def _comm(client: MagicMock) -> ProxmoxGuestAgentCommunicator:
    return ProxmoxGuestAgentCommunicator(
        client=client, node="pve01", vmid=999,
    )


# =====================================================================
# wait_ready
# =====================================================================


class TestWaitReady:
    def test_returns_on_first_successful_ping(self) -> None:
        client, endpoints = _agent_call_recorder()
        # First (and only) ping succeeds — no exception → ready.
        _comm(client).wait_ready(timeout=5)
        endpoints["ping"].post.assert_called_once_with()

    def test_retries_until_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient errors during agent startup are swallowed and
        retried until ``ping`` finally answers."""
        import time

        client, endpoints = _agent_call_recorder()
        # First two calls raise (agent still booting), third succeeds.
        endpoints_post = MagicMock(side_effect=[
            RuntimeError("agent not connected"),
            RuntimeError("still booting"),
            None,
        ])
        endpoints["ping"].post = endpoints_post
        # No-op the inter-poll sleep.
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        _comm(client).wait_ready(timeout=10)
        assert endpoints_post.call_count == 3

    def test_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time

        client, endpoints = _agent_call_recorder()
        endpoints["ping"].post.side_effect = RuntimeError("never ready")

        # Fake clock that ticks past the deadline immediately so
        # the test doesn't actually wait.
        clock = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        def _step(_):
            clock[0] += 5  # bigger than the 1.0 poll interval
        monkeypatch.setattr(time, "sleep", _step)

        with pytest.raises(VMTimeoutError, match="not ready"):
            _comm(client).wait_ready(timeout=3)


# =====================================================================
# exec
# =====================================================================


class TestExec:
    def test_simple_command_round_trip(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time

        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {"pid": 42}
        endpoints["exec-status"].get.return_value = {
            "exited": 1,
            "exitcode": 0,
            "out-data": "hello\n",  # PVE returns already-decoded text
            "err-data": "",
        }
        # Skip the 1s sleep between status polls.
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        result = _comm(client).exec(["echo", "hello"])

        assert isinstance(result, ExecResult)
        assert result.exit_code == 0
        assert result.stdout == b"hello\n"
        assert result.stderr == b""
        endpoints["exec"].post.assert_called_once_with(
            command=["echo", "hello"],
        )
        endpoints["exec-status"].get.assert_called_with(pid=42)

    def test_env_forwarded(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time
        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {"pid": 1}
        endpoints["exec-status"].get.return_value = {
            "exited": 1, "exitcode": 0, "out-data": "", "err-data": "",
        }
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        _comm(client).exec(
            ["printenv", "FOO"],
            env={"FOO": "bar", "BAZ": "qux"},
        )

        kwargs = endpoints["exec"].post.call_args.kwargs
        assert sorted(kwargs["env"]) == ["BAZ=qux", "FOO=bar"]

    def test_polls_until_exited(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import time
        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {"pid": 7}
        endpoints["exec-status"].get.side_effect = [
            {"exited": 0},
            {"exited": 0},
            {"exited": 1, "exitcode": 0, "out-data": "ok", "err-data": ""},
        ]
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        result = _comm(client).exec(["sleep", "0"])
        assert result.exit_code == 0
        assert endpoints["exec-status"].get.call_count == 3

    def test_empty_argv_raises(self) -> None:
        client, _ = _agent_call_recorder()
        with pytest.raises(GuestAgentError, match="non-empty"):
            _comm(client).exec([])

    def test_missing_pid_raises(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {}  # no pid in response
        with pytest.raises(GuestAgentError, match="no pid"):
            _comm(client).exec(["uname"])

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import time
        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {"pid": 1}
        endpoints["exec-status"].get.return_value = {"exited": 0}

        clock = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        def _step(_):
            clock[0] += 5
        monkeypatch.setattr(time, "sleep", _step)

        with pytest.raises(VMTimeoutError, match="timed out"):
            _comm(client).exec(["sleep", "999"], timeout=3)

    def test_output_that_looks_like_base64_is_kept_as_text(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: ``exec-status`` ``out-data`` is already-
        decoded text on PVE 9.x.  A previous cut tried base64-
        decoding first and corrupted ASCII outputs whose chars
        happened to be valid base64 (4-char-aligned, alphabet
        match) — ``"OKOK"`` decoded to ``b"\\xa3\\xa3\\xa3"``,
        ``"data"`` to ``b"u\\xab^"``, etc.  Verify text passes
        through verbatim regardless of how base64-shaped it looks.
        """
        import time
        client, endpoints = _agent_call_recorder()
        endpoints["exec"].post.return_value = {"pid": 1}
        # ``OKOK`` is 4 chars in the base64 alphabet — base64-
        # decodes happily but the actual command output is the
        # literal four-byte ASCII.
        endpoints["exec-status"].get.return_value = {
            "exited": 1,
            "exitcode": 0,
            "out-data": "OKOK",
            "err-data": "",
        }
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        result = _comm(client).exec(["echo", "OKOK"])
        assert result.stdout == b"OKOK"


# =====================================================================
# get_file
# =====================================================================


class TestGetFile:
    def test_returns_content_bytes(self) -> None:
        # PVE base64-encodes ``content`` on the wire even though it
        # base64-decodes ``out-data`` / ``err-data`` itself.  The
        # asymmetry is upstream behaviour; ``get_file`` must always
        # decode.
        client, endpoints = _agent_call_recorder()
        encoded = base64.b64encode(b"line one\nline two\n").decode("ascii")
        endpoints["file-read"].get.return_value = {"content": encoded}

        result = _comm(client).get_file("/etc/hosts")
        assert result == b"line one\nline two\n"
        endpoints["file-read"].get.assert_called_once_with(
            file="/etc/hosts",
        )

    def test_returns_binary_bytes(self) -> None:
        # Binary file (NUL bytes, non-UTF-8) round-trips correctly
        # via the always-decode path — earlier "guess if it's
        # base64" coercion would corrupt these.
        client, endpoints = _agent_call_recorder()
        encoded = base64.b64encode(b"\x00\xff\xc3\x28").decode("ascii")
        endpoints["file-read"].get.return_value = {"content": encoded}

        assert _comm(client).get_file("/bin/x") == b"\x00\xff\xc3\x28"

    def test_truncated_response_raises(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["file-read"].get.return_value = {
            "content": "first 16 MiB...",
            "truncated": 1,
        }
        with pytest.raises(GuestAgentError, match="truncated"):
            _comm(client).get_file("/var/log/huge")

    def test_proxmoxer_error_wrapped(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["file-read"].get.side_effect = RuntimeError("404")
        with pytest.raises(GuestAgentError, match="file-read failed"):
            _comm(client).get_file("/nope")


# =====================================================================
# put_file
# =====================================================================


class TestPutFile:
    def test_small_payload_single_call(self) -> None:
        client, endpoints = _agent_call_recorder()
        _comm(client).put_file("/tmp/x", b"hello\n")

        kwargs = endpoints["file-write"].post.call_args.kwargs
        assert kwargs["file"] == "/tmp/x"
        assert kwargs["encode"] == 1
        assert base64.b64decode(kwargs["content"]) == b"hello\n"

    def test_oversize_payload_raises(self) -> None:
        client, _ = _agent_call_recorder()
        # 50 KiB > the 48 KiB single-call cap.
        with pytest.raises(GuestAgentError, match="exceeds"):
            _comm(client).put_file("/tmp/x", b"a" * 50000)


# =====================================================================
# hostname
# =====================================================================


class TestHostname:
    def test_normal_response_shape(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["get-host-name"].get.return_value = {
            "result": {"host-name": "webpublic"},
        }
        assert _comm(client).hostname() == "webpublic"

    def test_legacy_flat_shape(self) -> None:
        """A few older PVE releases bubble ``host-name`` to the top
        level instead of nesting under ``result``.  Handle both."""
        client, endpoints = _agent_call_recorder()
        endpoints["get-host-name"].get.return_value = {
            "host-name": "legacy-host",
        }
        assert _comm(client).hostname() == "legacy-host"

    def test_unexpected_shape_raises(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["get-host-name"].get.return_value = {"oops": "no name"}
        with pytest.raises(GuestAgentError, match="unexpected shape"):
            _comm(client).hostname()


# =====================================================================
# _text_payload_to_bytes / _b64_payload_to_bytes corner cases
# (covered indirectly above; assert the shape contract directly here)
# =====================================================================


class TestTextPayloadToBytes:
    def test_empty_string_returns_empty_bytes(self) -> None:
        assert _text_payload_to_bytes("") == b""

    def test_bytes_pass_through(self) -> None:
        assert _text_payload_to_bytes(b"hello") == b"hello"

    def test_plain_text_utf8_encoded(self) -> None:
        assert _text_payload_to_bytes("hello, world") == b"hello, world"

    def test_base64_shaped_text_kept_verbatim(self) -> None:
        # Critical regression: a string whose chars happen to be in
        # the base64 alphabet (``OKOK``, ``data``, ``true``) must
        # NOT be base64-decoded.  An earlier cut tried base64 first
        # and corrupted command outputs whose bytes matched.
        for s in ("OKOK", "data", "true", "abcd"):
            assert _text_payload_to_bytes(s) == s.encode("utf-8")

    def test_non_utf8_text_does_not_raise(self) -> None:
        # rare cmd.exe / Windows-side junk in the string; must not
        # raise a UnicodeEncodeError.  ``errors="replace"`` on
        # encode emits ``b"?"`` for unencodable codepoints.
        result = _text_payload_to_bytes("\udcff")  # lone surrogate
        assert isinstance(result, bytes)


class TestB64PayloadToBytes:
    def test_empty_string_returns_empty_bytes(self) -> None:
        assert _b64_payload_to_bytes("") == b""

    def test_bytes_pass_through(self) -> None:
        assert _b64_payload_to_bytes(b"hello") == b"hello"

    def test_decodes_base64(self) -> None:
        encoded = base64.b64encode(b"binary\x00data").decode("ascii")
        assert _b64_payload_to_bytes(encoded) == b"binary\x00data"

    def test_invalid_base64_raises(self) -> None:
        # Strict mode — a clearly-invalid string raises rather than
        # returning silently corrupted output.
        with pytest.raises(GuestAgentError, match="not valid base64"):
            _b64_payload_to_bytes("not!valid$base64")

    def test_non_string_raises(self) -> None:
        with pytest.raises(GuestAgentError, match="expected base64"):
            _b64_payload_to_bytes(123)
