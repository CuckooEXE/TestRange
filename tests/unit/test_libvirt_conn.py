"""Libvirt connection plumbing — C-level error routing (CORE-81).

libvirt prints errors to fd 2 by default, which corrupts the live dashboard's
``Live`` region (the reported flicker / ``libvirt: …`` glimpses). The driver
registers a handler that routes them through Python logging instead.
"""

from __future__ import annotations

import logging
import threading
import time
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


def test_event_loop_registers_once_and_pumps(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nonblocking virStream I/O (the BACKEND-5 console sink) is deaf without a
    # registered + running event loop: stream data and the power-off EOF arrive
    # only through it (live-found: remote builds sat would-blocked for the whole
    # build timeout). Registration must happen exactly once per process.
    monkeypatch.setattr(_conn, "_event_loop_running", False)
    registered: list[bool] = []
    pumped = threading.Event()

    def _run_impl() -> None:
        pumped.set()
        time.sleep(0.05)  # keep the daemon thread tame while the test asserts

    fake = SimpleNamespace(
        virEventRegisterDefaultImpl=lambda: registered.append(True),
        virEventRunDefaultImpl=_run_impl,
    )
    _conn._ensure_event_loop(fake)
    _conn._ensure_event_loop(fake)
    assert registered == [True], "event impl must register exactly once"
    assert pumped.wait(2.0), "pump thread never ran virEventRunDefaultImpl"


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
