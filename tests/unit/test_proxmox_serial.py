"""PVE-17: the Proxmox build-result sink (serial0 over termproxy->vncwebsocket).

Two layers, both faked so no PVE or real socket is touched:

* ``_serial.read_build_result_sink`` — the generator the orchestrator tails:
  frame streaming, idle heartbeat + keepalive, EOF on close, deterministic
  teardown (the websocket is closed even on an early break).
* ``_client.open_serial_websocket`` — the transport open: termproxy POST, the
  ``vncwebsocket`` URL / cookie / origin, the auth handshake, and the
  password-ticket-only guard.
"""

from __future__ import annotations

import ssl
from contextlib import closing
from typing import Any

import pytest
import websocket

from testrange.drivers.proxmox import _serial, _vm
from testrange.drivers.proxmox._client import ProxmoxClient, ProxmoxConn
from testrange.exceptions import DriverError

_TIMEOUT = object()  # script sentinel: recv() raises a read timeout
_CLOSED = object()  # script sentinel: recv() raises a connection-closed


class _FakeWS:
    """A websocket that replays a scripted sequence of recv() outcomes."""

    def __init__(self, script: list[Any], *, raise_on_send: bool = False) -> None:
        self._script = list(script)
        self._raise_on_send = raise_on_send
        self.sent: list[str] = []
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, t: float) -> None:
        self.timeout = t

    def send(self, data: str) -> None:
        if self._raise_on_send:
            raise websocket.WebSocketException("simulated keepalive send failure")
        self.sent.append(data)

    def recv(self) -> bytes | str:
        if not self._script:
            raise websocket.WebSocketConnectionClosedException()
        item = self._script.pop(0)
        if item is _TIMEOUT:
            raise websocket.WebSocketTimeoutException()
        if item is _CLOSED:
            raise websocket.WebSocketConnectionClosedException()
        return item  # type: ignore[no-any-return]

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Stands in for ProxmoxClient: just hands back a prepared websocket."""

    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws
        self.opened_vmid: int | None = None

    def open_serial_websocket(self, vmid: int) -> _FakeWS:
        self.opened_vmid = vmid
        return self._ws


@pytest.fixture(autouse=True)
def _fixed_vmid(monkeypatch: pytest.MonkeyPatch) -> None:
    # vmid resolution is covered in test_proxmox_vm; pin it here so these tests
    # focus on the serial streaming.
    monkeypatch.setattr(_vm, "resolve_vmid", lambda client, name: 100)


def _drain(client: Any) -> list[bytes]:
    out: list[bytes] = []
    with closing(_serial.read_build_result_sink(client, "tr_build_vm_x")) as gen:
        out.extend(gen)
    return out


class TestReadBuildResultSink:
    def test_streams_frames_then_eof(self) -> None:
        ws = _FakeWS([b"boot chatter\n", b"Setting up nginx\n", b"TESTRANGE-RESULT: ok\n"])
        client = _FakeClient(ws)
        assert _drain(client) == [
            b"boot chatter\n",
            b"Setting up nginx\n",
            b"TESTRANGE-RESULT: ok\n",
        ]
        assert client.opened_vmid == 100  # resolved vmid threaded to the client
        assert ws.timeout == _serial._RECV_TIMEOUT_S  # heartbeat cadence set
        assert ws.closed  # generator drained -> finally closed the socket

    def test_idle_yields_heartbeat_and_keepalive(self) -> None:
        ws = _FakeWS([_TIMEOUT, b"TESTRANGE-RESULT: ok\n"])
        chunks = _drain(_FakeClient(ws))
        assert chunks == [b"", b"TESTRANGE-RESULT: ok\n"]  # b"" heartbeat on the idle tick
        assert ws.sent == [_serial._KEEPALIVE_FRAME]  # nudged PVE's idle culler

    def test_text_frame_is_encoded_to_bytes(self) -> None:
        assert _drain(_FakeClient(_FakeWS(["plain text\n"]))) == [b"plain text\n"]

    def test_connection_closed_is_eof(self) -> None:
        ws = _FakeWS([b"partial output\n", _CLOSED])
        assert _drain(_FakeClient(ws)) == [b"partial output\n"]
        assert ws.closed

    def test_empty_frame_yields_heartbeat_not_eof(self) -> None:
        # ORCH-7 + PVE-29: an empty *data* frame (e.g. a keepalive echo) must NOT
        # end the stream — only a real close (the exception) does. It now yields a
        # b"" heartbeat (not a bare skip) so the orchestrator's deadline check
        # still ticks; a steady trickle of empty frames would otherwise busy-spin.
        ws = _FakeWS([b"a\n", b"", b"TESTRANGE-RESULT: ok\n"])
        assert _drain(_FakeClient(ws)) == [b"a\n", b"", b"TESTRANGE-RESULT: ok\n"]

    def test_keepalive_failure_raises_not_silent_eof(self) -> None:
        # PVE-29: a keepalive-send failure is a transport death, not a guest
        # poweroff. It must raise (so the orchestrator sees a transport error)
        # rather than exhaust the generator — which would be misread as "console
        # closed without ok" and fail a possibly-healthy build on a network blip.
        ws = _FakeWS([_TIMEOUT, b"TESTRANGE-RESULT: ok\n"], raise_on_send=True)
        with pytest.raises(DriverError, match="serial transport"):
            _drain(_FakeClient(ws))
        assert ws.closed  # the generator's finally still released the socket

    def test_socket_closed_even_on_early_break(self) -> None:
        # The orchestrator breaks out the moment it parses a record; closing()
        # must still run the generator's finally and release the socket.
        ws = _FakeWS([b"first\n", b"second\n", b"third\n"])
        client: Any = _FakeClient(ws)
        with closing(_serial.read_build_result_sink(client, "n")) as gen:
            assert next(iter(gen)) == b"first\n"
        assert ws.closed


# -- _client.open_serial_websocket ----------------------------------------


class _TermproxyEndpoint:
    def __init__(self, api: _FakeSerialApi) -> None:
        self._api = api

    def post(self) -> dict[str, Any]:
        self._api.posted = True
        return self._api.termproxy_resp


class _QemuEndpoint:
    def __init__(self, api: _FakeSerialApi) -> None:
        self._api = api

    @property
    def termproxy(self) -> _TermproxyEndpoint:
        return _TermproxyEndpoint(self._api)


class _NodesEndpoint:
    def __init__(self, api: _FakeSerialApi) -> None:
        self._api = api

    def qemu(self, vmid: int) -> _QemuEndpoint:
        self._api.qemu_vmid = vmid
        return _QemuEndpoint(self._api)


class _FakeSerialApi:
    def __init__(self, tokens: tuple[str | None, str | None] = ("SESSION-TICKET", "CSRF")) -> None:
        self._tokens = tokens
        self.termproxy_resp: dict[str, Any] = {
            "port": 5900,
            "ticket": "PVEVNC:abc",
            "user": "root@pam",
        }
        self.posted = False
        self.qemu_vmid: int | None = None
        self.node_arg: str | None = None

    def get_tokens(self) -> tuple[str | None, str | None]:
        return self._tokens

    def nodes(self, node: str) -> _NodesEndpoint:
        self.node_arg = node
        return _NodesEndpoint(self)


class _HandshakeWS:
    def __init__(self, reply: bytes | str = b"OK") -> None:
        self._reply = reply
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> bytes | str:
        return self._reply

    def close(self) -> None:
        self.closed = True


def _client_with(api: _FakeSerialApi) -> ProxmoxClient:
    conn = ProxmoxConn(
        host="pve.example", node="n1", user="root@pam", password="pw", verify_ssl=False
    )
    client = ProxmoxClient(conn)
    client._api = api  # bypass connect(); inject the fake REST handle
    return client


class TestOpenSerialWebsocket:
    def test_builds_url_cookie_origin_and_authenticates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeSerialApi()
        captured: dict[str, Any] = {}
        ws = _HandshakeWS()

        def _fake_create(url: str, **kwargs: Any) -> _HandshakeWS:
            captured["url"] = url
            captured.update(kwargs)
            return ws

        monkeypatch.setattr("websocket.create_connection", _fake_create)
        returned = _client_with(api).open_serial_websocket(100)

        assert returned is ws
        assert api.posted and api.qemu_vmid == 100 and api.node_arg == "n1"
        # vncticket URL-encoded, port from the termproxy response.
        assert captured["url"] == (
            "wss://pve.example:8006/api2/json/nodes/n1/qemu/100/vncwebsocket"
            "?port=5900&vncticket=PVEVNC%3Aabc"
        )
        assert captured["header"] == ["Cookie: PVEAuthCookie=SESSION-TICKET"]
        assert captured["origin"] == "https://pve.example:8006"
        assert captured["sslopt"] == {"cert_reqs": ssl.CERT_NONE}
        # Auth frame is "user:vncticket\n"; server replied OK.
        assert ws.sent == ["root@pam:PVEVNC:abc\n"]

    def test_uses_resolved_node_under_autodetect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-24: with node auto-detect, conn.node is "" and the *resolved* node
        # (client.node) must drive the termproxy path — else the node segment is
        # empty (POST /nodes//qemu/… → 501). Earlier tests used an explicit node
        # so couldn't catch this.
        api = _FakeSerialApi()
        captured: dict[str, Any] = {}

        def _fake_create(url: str, **kwargs: Any) -> _HandshakeWS:
            captured["url"] = url
            return _HandshakeWS()

        monkeypatch.setattr("websocket.create_connection", _fake_create)
        conn = ProxmoxConn(host="pve.example", node="", password="pw", verify_ssl=False)
        client = ProxmoxClient(conn)
        client._api = api
        client._node = "autonode"  # what connect() resolved (conn.node stayed "")
        client.open_serial_websocket(100)
        assert api.node_arg == "autonode"
        assert "/nodes/autonode/qemu/100/vncwebsocket" in captured["url"]

    def test_api_token_auth_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # get_tokens() returns (None, None) for API-token auth — termproxy can't
        # use it, so fail loud before opening anything.
        api = _FakeSerialApi(tokens=(None, None))
        monkeypatch.setattr(
            "websocket.create_connection", lambda *a, **k: pytest.fail("should not connect")
        )
        with pytest.raises(DriverError, match="password-ticket"):
            _client_with(api).open_serial_websocket(100)

    def test_bad_handshake_reply_raises_and_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _FakeSerialApi()
        ws = _HandshakeWS(reply=b"ERR permission denied")
        monkeypatch.setattr("websocket.create_connection", lambda *a, **k: ws)
        with pytest.raises(DriverError, match="auth rejected"):
            _client_with(api).open_serial_websocket(100)
        assert ws.closed  # the half-open socket is released on the failure path
