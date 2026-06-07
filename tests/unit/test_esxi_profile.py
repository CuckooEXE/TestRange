"""ESXI-1: ESXiProfile parsing + EsxiConn URI round-trip + registration."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import _PROFILE_BY_SCHEME
from testrange.drivers._registry import _FROM_NAME, _SCHEME_FOR_HYP
from testrange.drivers.esxi import ESXiDriver, ESXiHypervisor, ESXiProfile
from testrange.drivers.esxi._client import EsxiConn
from testrange.exceptions import ProfileError


class TestRegistration:
    def test_driver_and_scheme_registered(self) -> None:
        assert "ESXiDriver" in _FROM_NAME
        assert _SCHEME_FOR_HYP.get(ESXiHypervisor) == "esxi"

    def test_profile_registered(self) -> None:
        assert _PROFILE_BY_SCHEME.get("esxi") is ESXiProfile


class TestEsxiConn:
    def test_uri_round_trip(self) -> None:
        conn = EsxiConn(host="h", user="root", password="p@ss!", datastore="ds2", port=8443)
        back = EsxiConn.from_uri(conn.to_uri())
        assert back == conn

    def test_from_uri_defaults(self) -> None:
        conn = EsxiConn.from_uri("esxi://root@1.2.3.4/")
        assert conn.host == "1.2.3.4"
        assert conn.datastore == "datastore1"
        assert conn.port == 443
        assert conn.verify_ssl is False

    def test_from_uri_rejects_wrong_scheme(self) -> None:
        from testrange.exceptions import DriverError

        with pytest.raises(DriverError, match="expected an esxi://"):
            EsxiConn.from_uri("proxmox://root@h/")


class TestProfileFromTable:
    def test_minimal(self) -> None:
        p = ESXiProfile._from_table({"host": "1.2.3.4"}, Path("conn.toml"))
        assert p.host == "1.2.3.4" and p.user == "root" and p.datastore == "datastore1"

    def test_full_with_uplinks(self) -> None:
        table = {
            "host": "h",
            "user": "root",
            "password": "pw",
            "datastore": "ds2",
            "port": 8443,
            "verify_ssl": True,
            "uplinks": {"egress": "vmnic1"},
        }
        p = ESXiProfile._from_table(table, Path("conn.toml"))
        assert p.port == 8443 and p.verify_ssl is True
        assert dict(p.uplinks) == {"egress": "vmnic1"}

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ProfileError):
            ESXiProfile._from_table({"host": "h", "bogus": 1}, Path("conn.toml"))

    def test_missing_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="host must be"):
            ESXiProfile(host="")

    def test_build_driver(self) -> None:
        p = ESXiProfile._from_table(
            {"host": "h", "password": "pw", "uplinks": {"egress": "vmnic1"}}, Path("c.toml")
        )
        driver = p.build_driver()
        assert isinstance(driver, ESXiDriver)
        assert driver._uplinks == {"egress": "vmnic1"}
        assert driver.uri.startswith("esxi://root:pw@h")
