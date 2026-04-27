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
    _coerce_output,
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

    def test_base64_encoded_output_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Older PVE releases handed back base64-wrapped data
        instead of decoded text.  ``_coerce_output`` should
        transparently decode it."""
        import time
        client, endpoints = _agent_call_recorder()
        encoded = base64.b64encode(b"binary\x00bytes").decode("ascii")
        endpoints["exec"].post.return_value = {"pid": 1}
        endpoints["exec-status"].get.return_value = {
            "exited": 1,
            "exitcode": 0,
            "out-data": encoded,
            "err-data": "",
        }
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        result = _comm(client).exec(["cat", "/file"])
        assert result.stdout == b"binary\x00bytes"


# =====================================================================
# get_file
# =====================================================================


class TestGetFile:
    def test_returns_content_bytes(self) -> None:
        client, endpoints = _agent_call_recorder()
        endpoints["file-read"].get.return_value = {
            "content": "line one\nline two\n",
        }

        result = _comm(client).get_file("/etc/hosts")
        assert result == b"line one\nline two\n"
        endpoints["file-read"].get.assert_called_once_with(
            file="/etc/hosts",
        )

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
# _coerce_output corner cases (covered indirectly above; assert the
# shape contract directly here)
# =====================================================================


class TestCoerceOutput:
    def test_empty_string_returns_empty_bytes(self) -> None:
        assert _coerce_output("") == b""

    def test_bytes_pass_through(self) -> None:
        assert _coerce_output(b"hello") == b"hello"

    def test_base64_decoded(self) -> None:
        encoded = base64.b64encode(b"binary\x00data").decode("ascii")
        assert _coerce_output(encoded) == b"binary\x00data"

    def test_plain_text_falls_back_to_utf8(self) -> None:
        """Strings that fail strict base64 are encoded as UTF-8 so
        callers always see bytes."""
        assert _coerce_output("hello, world") == b"hello, world"
