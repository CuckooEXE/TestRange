"""Libvirt connection plumbing — C-level error routing (CORE-81).

libvirt prints errors to fd 2 by default, which corrupts the live dashboard's
``Live`` region (the reported flicker / ``libvirt: …`` glimpses). The driver
registers a handler that routes them through Python logging instead.
"""

from __future__ import annotations

import logging
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
