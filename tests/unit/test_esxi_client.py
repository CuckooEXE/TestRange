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
