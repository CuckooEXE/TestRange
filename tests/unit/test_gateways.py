"""Tests for the pluggable guest-reachability gateways.

``open_socket`` wiring is checked against a mocked paramiko; the local-forward
pump is checked end-to-end against a real loopback echo server (with the bastion
channel stubbed to a real socket, so the threaded pump is genuinely exercised).
"""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange.exceptions import GatewayError
from testrange.gateways import GuestGateway, SSHJumpGateway, ssh_jump


class _FakeTransport:
    def __init__(self) -> None:
        self.channels: list[tuple[str, tuple[str, int], tuple[str, int]]] = []

    def open_channel(
        self, kind: str, dest: tuple[str, int], src: tuple[str, int]
    ) -> tuple[str, int]:
        self.channels.append((kind, dest, src))
        return dest  # stand in for the channel; identity is enough to assert wiring


class _FakeJumpClient:
    def __init__(self) -> None:
        self.connect_args: dict[str, Any] = {}
        self.connect_calls = 0
        self.closed = False
        self._transport = _FakeTransport()

    def set_missing_host_key_policy(self, _p: Any) -> None:
        pass

    def connect(self, **kwargs: Any) -> None:
        self.connect_calls += 1
        self.connect_args = kwargs

    def get_transport(self) -> _FakeTransport:
        return self._transport

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_paramiko(monkeypatch: pytest.MonkeyPatch) -> _FakeJumpClient:
    client = _FakeJumpClient()
    mod = MagicMock()
    mod.SSHClient.return_value = client
    mod.AutoAddPolicy = MagicMock
    mod.SSHException = Exception
    monkeypatch.setattr("testrange.gateways.ssh_jump._import_paramiko", lambda: mod)
    return client


def test_ssh_jump_is_a_guest_gateway() -> None:
    assert issubclass(SSHJumpGateway, GuestGateway)


def test_open_socket_tunnels_direct_tcpip_to_guest(fake_paramiko: _FakeJumpClient) -> None:
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    chan = gw.open_socket("10.30.0.41", 22)
    assert fake_paramiko._transport.channels == [
        ("direct-tcpip", ("10.30.0.41", 22), ("127.0.0.1", 0))
    ]
    assert chan == ("10.30.0.41", 22)
    assert fake_paramiko.connect_args["hostname"] == "bastion"
    assert fake_paramiko.connect_args["username"] == "root"
    assert fake_paramiko.connect_args["password"] == "pw"


def test_jump_connection_is_established_once_and_multiplexed(
    fake_paramiko: _FakeJumpClient,
) -> None:
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    gw.open_socket("10.30.0.41", 22)
    gw.open_socket("10.30.0.120", 22)
    assert fake_paramiko.connect_calls == 1  # one bastion connection
    assert len(fake_paramiko._transport.channels) == 2  # two tunnels over it


def test_close_releases_the_jump(fake_paramiko: _FakeJumpClient) -> None:
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    gw.open_socket("10.30.0.41", 22)
    gw.close()
    assert fake_paramiko.closed is True
    gw.close()  # idempotent


def test_deliberate_reopen_after_close_redials_a_tracked_jump(
    fake_paramiko: _FakeJumpClient,
) -> None:
    # close() is not terminal (PROXY-3): the Communicator close()/reconnect
    # contract rides the gateway, so a deliberate open after close() must lazily
    # re-establish the jump — and the new client must be tracked (closed by the
    # next close()), not a leaked side-channel.
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    gw.open_socket("10.30.0.41", 22)
    gw.close()
    assert fake_paramiko.closed is True
    fake_paramiko.closed = False
    gw.open_socket("10.30.0.41", 22)
    assert fake_paramiko.connect_calls == 2  # genuinely redialled
    gw.close()
    assert fake_paramiko.closed is True  # the reopened client is tracked


def test_stale_serve_generation_is_refused_after_close(fake_paramiko: _FakeJumpClient) -> None:
    # A local-forward _serve thread can win the accept() race against close()
    # and re-dial; it carries the generation it was spawned under, and a stale
    # generation must be refused so it cannot resurrect an untracked bastion
    # client (use-after-close, PROXY-2). _serve dials with generation= — mirror
    # that call shape.
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    gw.open_socket("10.30.0.41", 22)
    stale = gw._generation
    gw.close()
    with pytest.raises(GatewayError, match="closed"):
        gw._channel_to("10.30.0.41", 22, ("127.0.0.1", 0), generation=stale)
    assert fake_paramiko.connect_calls == 1  # no resurrection happened


def test_missing_credentials_raises_gateway_error(fake_paramiko: _FakeJumpClient) -> None:
    gw = SSHJumpGateway(host="bastion", username="root")  # no password, no pkey
    with pytest.raises(GatewayError):
        gw.open_socket("10.30.0.41", 22)


def _echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            data = conn.recv(1024)
            if data:
                conn.sendall(data)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_local_forward_pumps_bytes_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real loopback "guest" (echo server); the bastion channel is stubbed to a
    # real socket connected to it, so open_local_forward's listener + threaded
    # pump are exercised genuinely (not mocked).
    echo_srv, echo_port = _echo_server()
    gw = SSHJumpGateway(host="bastion", username="root", password="pw")
    monkeypatch.setattr(gw, "_ensure_jump", lambda: object())

    def _real_channel(
        host: str, port: int, origin: tuple[str, int], *, generation: int | None = None
    ) -> socket.socket:
        return socket.create_connection(("127.0.0.1", echo_port), timeout=5)

    monkeypatch.setattr(gw, "_channel_to", _real_channel)
    try:
        local_port = gw.open_local_forward("10.30.0.41", 9999)
        client = socket.create_connection(("127.0.0.1", local_port), timeout=5)
        client.settimeout(5)
        client.sendall(b"ping-through-jump")
        assert client.recv(1024) == b"ping-through-jump"
        client.close()
    finally:
        gw.close()
        echo_srv.close()


class _FakeSSHException(Exception):
    pass


class _UnparseableKey:
    @classmethod
    def from_private_key(cls, _f: Any) -> Any:
        raise _FakeSSHException("not this key type")


class _ParseableKey:
    @classmethod
    def from_private_key(cls, _f: Any) -> Any:
        return "PARSED-KEY"


def _fake_paramiko(**key_classes: Any) -> Any:
    return SimpleNamespace(SSHException=_FakeSSHException, **key_classes)


class TestLoadPrivateKey:
    def test_unparseable_key_raises_gateway_error(self) -> None:
        fake = _fake_paramiko(
            Ed25519Key=_UnparseableKey,
            RSAKey=_UnparseableKey,
            ECDSAKey=_UnparseableKey,
            DSSKey=_UnparseableKey,
        )
        with pytest.raises(GatewayError, match="could not parse jump private key"):
            ssh_jump._load_private_key("garbage", fake)

    def test_falls_through_to_a_later_key_type(self) -> None:
        # Ed25519 rejects it, RSA parses it — the loop must try the next type,
        # not bail on the first SSHException.
        fake = _fake_paramiko(
            Ed25519Key=_UnparseableKey,
            RSAKey=_ParseableKey,
            ECDSAKey=_UnparseableKey,
            DSSKey=_UnparseableKey,
        )
        assert ssh_jump._load_private_key("pem-text", fake) == "PARSED-KEY"
