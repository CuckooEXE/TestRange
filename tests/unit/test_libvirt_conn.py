"""Libvirt connection plumbing — C-level error routing (CORE-81).

libvirt prints errors to fd 2 by default, which corrupts the live dashboard's
``Live`` region (the reported flicker / ``libvirt: …`` glimpses). The driver
registers a handler that routes them through Python logging instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import stat
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from testrange.drivers.libvirt import _conn

_LOGGER = "testrange.drivers.libvirt._conn"

_Errback = Callable[[object, tuple[Any, ...]], None]


class _Sink(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _reset_handler_flag() -> Iterator[None]:
    # The registration is process-global; reset around each test.
    _conn._error_handler_registered = False
    yield
    _conn._error_handler_registered = False


def test_registers_a_handler_that_logs_instead_of_stderr() -> None:
    captured: dict[str, _Errback] = {}

    # registerErrorHandler(f, ctx): the callback is the FIRST positional arg.
    def _register(cb: _Errback, _ctx: object) -> None:
        captured["cb"] = cb

    _conn._route_libvirt_errors_to_log(SimpleNamespace(registerErrorHandler=_register))
    assert "cb" in captured, "no handler registered → libvirt keeps printing to stderr"

    # Capture on the _conn logger directly (independent of propagation, which a
    # prior test's configure() may have turned off on the testrange tree).
    logger = logging.getLogger(_LOGGER)
    sink = _Sink()
    logger.addHandler(sink)
    prev = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        # libvirt-shaped error: (code, domain, message, level, str1, str2, str3, int1, int2)
        captured["cb"](None, (38, 10, "Domain not found", 2, "", "", "", 0, 0))
    finally:
        logger.removeHandler(sink)
        logger.setLevel(prev)
    assert any("Domain not found" in r.getMessage() for r in sink.records)


def test_registration_is_idempotent() -> None:
    calls: list[object] = []
    fake = SimpleNamespace(registerErrorHandler=lambda cb, _ctx: calls.append(cb))
    _conn._route_libvirt_errors_to_log(fake)
    _conn._route_libvirt_errors_to_log(fake)
    assert len(calls) == 1  # registered once; re-imports are no-ops


class TestTeardownUri:
    """``to_uri``/``from_uri`` is the state.json round-trip that `testrange
    cleanup` reaches a torn-down run by — a regression silently breaks cleanup."""

    def test_round_trips_default_uri(self) -> None:
        c = _conn.LibvirtConn()
        assert _conn.LibvirtConn.from_uri(c.to_uri()) == c

    def test_round_trips_custom_uri(self) -> None:
        c = _conn.LibvirtConn(libvirt_uri="qemu+ssh://root@host:22/system")
        assert _conn.LibvirtConn.from_uri(c.to_uri()) == c

    def test_wrong_scheme_raises(self) -> None:
        from testrange.exceptions import DriverError

        with pytest.raises(DriverError, match="teardown URI"):
            _conn.LibvirtConn.from_uri("qemu:///system")


class TestSerialListener:
    """The build-result serial socket's hardening (CORE-91)."""

    def test_serial_dir_is_not_world_listable(self) -> None:
        # The daemon only needs to *traverse* the dir (connect to a known path),
        # not *list* it; dropping o+r denies a co-tenant cheap enumeration.
        client = _conn.LibvirtClient(_conn.LibvirtConn())
        d = client._ensure_serial_dir()
        try:
            assert stat.S_IMODE(d.stat().st_mode) == 0o711
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_allowed_peer_uids_include_self_and_root(self) -> None:
        uids = _conn._allowed_serial_peer_uids()
        assert os.getuid() in uids
        assert 0 in uids

    def test_accept_serial_accepts_an_allowed_peer(self) -> None:
        # A connection from this same process has peer uid == our uid (allowed),
        # so the SO_PEERCRED filter lets it through — the legit-QEMU path.
        client = _conn.LibvirtClient(_conn.LibvirtConn())
        path = client.open_serial_listener("tr-vm-x")
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.connect(path)  # lands in the listen backlog
            conn = client.accept_serial("tr-vm-x", timeout=2.0)
            assert conn is not None
            assert _conn._peer_uid(conn) == os.getuid()
            conn.close()
        finally:
            peer.close()
            client.close_serial_listener("tr-vm-x")
            if client._serial_dir is not None:
                shutil.rmtree(client._serial_dir, ignore_errors=True)
