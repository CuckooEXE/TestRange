"""Tests for EsxiClient.connect() session lifecycle (ESXI-33)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange.drivers.esxi import _client


def test_connect_closes_session_on_inventory_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # SmartConnect opens an authenticated server-side session; if RetrieveContent
    # / inventory resolution then fails (e.g. a datastore not yet mounted during a
    # nested-ESXi boot), connect() must Disconnect it — otherwise wait_esxi_ready's
    # retry loop leaks one session per poll until the host's session limit is hit.
    si = SimpleNamespace()

    def boom() -> Any:
        raise RuntimeError("datastore not mounted yet")

    si.RetrieveContent = boom
    disconnected: list[Any] = []
    vim_connect = SimpleNamespace(
        SmartConnect=lambda **_kw: si,
        Disconnect=lambda s: disconnected.append(s),
    )
    monkeypatch.setattr(_client, "_import_pyvmomi", lambda: (vim_connect, MagicMock()))

    client = _client.EsxiClient(_client.EsxiConn(host="h", password="p"))
    with pytest.raises(RuntimeError, match="datastore not mounted"):
        client.connect()
    assert disconnected == [si]  # the leaked session was Disconnect()ed before re-raise


@pytest.mark.parametrize("status", [404, 416, 429, 503])
def test_folder_read_from_tolerates_transient_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # The serial-sink tail polls /folder once a second; 404/416 mean "file not
    # there / nothing new", and 503/429 mean hostd/envoy momentarily refused
    # under parallel build fan-out (live-found on a nested host, ESXI-37). All
    # are one missed poll — b"" heartbeat — never a build-phase crash; the
    # build-timeout watchdog still bounds a genuinely dead host.
    resp = SimpleNamespace(
        status_code=status,
        content=b"",
        raise_for_status=lambda: (_ for _ in ()).throw(AssertionError("must not be raised")),
    )
    fake_requests = SimpleNamespace(
        get=lambda *_a, **_kw: resp,
        auth=SimpleNamespace(HTTPBasicAuth=lambda _u, _p: None),
    )
    monkeypatch.setattr(_client, "_import_requests", lambda: fake_requests)
    client = _client.EsxiClient(_client.EsxiConn(host="h", password="p"))
    client._datacenter = SimpleNamespace(name="ha-datacenter")  # connect()-time state
    assert client.folder_read_from("vm/serial0.log", 0) == b""


def test_folder_read_from_still_raises_on_auth_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tolerance is scoped to transient statuses: a 401 (bad credentials)
    # must keep failing loud, not read as an idle poll forever.
    class _Boom(Exception):
        pass

    def _raise() -> None:
        raise _Boom("401")

    resp = SimpleNamespace(status_code=401, content=b"", raise_for_status=_raise)
    fake_requests = SimpleNamespace(
        get=lambda *_a, **_kw: resp,
        auth=SimpleNamespace(HTTPBasicAuth=lambda _u, _p: None),
    )
    monkeypatch.setattr(_client, "_import_requests", lambda: fake_requests)
    client = _client.EsxiClient(_client.EsxiConn(host="h", password="p"))
    client._datacenter = SimpleNamespace(name="ha-datacenter")  # connect()-time state
    with pytest.raises(_Boom):
        client.folder_read_from("vm/serial0.log", 0)
