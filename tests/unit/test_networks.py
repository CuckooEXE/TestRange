"""Tests for Network and Switch."""

from __future__ import annotations

import ipaddress

import pytest

from testrange.networks import Network, Switch


class TestNetwork:
    def test_valid(self) -> None:
        n = Network("netA", "172.31.0.0/24")
        assert n.name == "netA"
        assert n.cidr == "172.31.0.0/24"
        assert n.dhcp is True
        assert n.dns is True
        assert isinstance(n.network, ipaddress.IPv4Network)

    def test_dhcp_off(self) -> None:
        n = Network("netA", "10.0.0.0/24", dhcp=False)
        assert n.dhcp is False

    def test_bad_cidr(self) -> None:
        with pytest.raises(ValueError):
            Network("netA", "not-a-cidr")

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError):
            Network("", "10.0.0.0/24")


class TestSwitch:
    def test_variadic(self) -> None:
        sw = Switch(
            "sw1",
            Network("netA", "10.0.0.0/24"),
            Network("netB", "10.0.1.0/24"),
        )
        assert sw.name == "sw1"
        assert len(sw.networks) == 2

    def test_defaults(self) -> None:
        sw = Switch("sw1")
        assert sw.mgmt is False
        assert sw.internet is True

    def test_mgmt_flag(self) -> None:
        sw = Switch("sw1", mgmt=True)
        assert sw.mgmt is True

    def test_rejects_non_network(self) -> None:
        with pytest.raises(TypeError):
            Switch("sw1", "not a network")  # type: ignore[arg-type]
