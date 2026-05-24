"""Tests for Network / Switch / NetworkAddressing / validate_addressing."""

from __future__ import annotations

import dataclasses
import ipaddress

import pytest

from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.entry import CacheEntry
from testrange.communicators.ssh import SSHCommunicator
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr
from testrange.devices.network import NetworkIface
from testrange.networks import Network, NetworkAddressing, Switch
from testrange.networks.validate import validate_addressing
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec


class TestNetwork:
    def test_valid(self) -> None:
        n = Network("netA")
        assert n.name == "netA"

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError):
            Network("")

    def test_charset_not_policed_at_value_object(self) -> None:
        # Network is backend-agnostic: it only checks non-empty. The
        # libvirt-specific charset rule is enforced at MockHypervisor
        # (see tests/unit/test_plan.py), so a comma is fine here.
        assert Network("net,a").name == "net,a"


class TestSwitch:
    def test_variadic_default_cidr(self) -> None:
        sw = Switch("sw1", Network("netA"), Network("netB"))
        assert sw.name == "sw1"
        assert len(sw.networks) == 2
        assert sw.cidr == "192.168.10.0/24"
        assert sw.uplink is None
        assert sw.mgmt is False
        assert sw.dhcp is False
        assert sw.dns is False
        assert sw.nat is False

    def test_strict_cidr_rejects_host_form(self) -> None:
        with pytest.raises(ValueError, match=r"strict form|host address|network address"):
            Switch("sw1", Network("a"), cidr="192.168.10.1/24")

    def test_strict_cidr_accepts_network_form(self) -> None:
        sw = Switch("sw1", Network("a"), cidr="10.0.0.0/24")
        assert sw.cidr == "10.0.0.0/24"
        assert sw.network.network_address == ipaddress.IPv4Address("10.0.0.0")

    def test_ipv6_rejected(self) -> None:
        with pytest.raises(ValueError, match="IPv4"):
            Switch("sw1", Network("a"), cidr="fd00::/64")

    def test_nat_without_uplink_raises(self) -> None:
        with pytest.raises(ValueError, match=r"nat=True.*uplink"):
            Switch("sw1", Network("a"), nat=True)

    def test_nat_with_uplink_ok(self) -> None:
        sw = Switch("sw1", Network("a"), nat=True, uplink="eth0")
        assert sw.nat is True
        assert sw.uplink == "eth0"
        assert sw.needs_sidecar is True

    def test_empty_uplink_rejected(self) -> None:
        with pytest.raises(ValueError, match="uplink"):
            Switch("sw1", Network("a"), uplink="")

    def test_uplink_addr_ok_with_nat(self) -> None:
        # NET-7: static address for the sidecar's MASQUERADE uplink NIC.
        addr = StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",))
        sw = Switch("sw1", Network("a"), nat=True, uplink="vmbr9", uplink_addr=addr)
        assert sw.uplink_addr is addr

    def test_uplink_addr_requires_nat(self) -> None:
        with pytest.raises(ValueError, match=r"uplink_addr.*nat=True"):
            Switch("sw1", Network("a"), uplink="vmbr9", uplink_addr=StaticAddr("10.10.10.2/24"))

    def test_uplink_addr_requires_explicit_prefix(self) -> None:
        # The uplink is its own subnet (not the Switch CIDR), so the netmask
        # can't be derived — a bare address is rejected.
        with pytest.raises(ValueError, match="prefix"):
            Switch(
                "sw1", Network("a"), nat=True, uplink="vmbr9", uplink_addr=StaticAddr("10.10.10.2")
            )

    def test_pinned_addresses(self) -> None:
        sw = Switch("sw1", Network("a"), cidr="172.31.0.0/24")
        assert sw.sidecar_ip == "172.31.0.1"
        assert sw.mgmt_ip == "172.31.0.2"

    def test_needs_sidecar_flags(self) -> None:
        assert Switch("s", Network("a")).needs_sidecar is False
        assert Switch("s", Network("a"), dhcp=True).needs_sidecar is True
        assert Switch("s", Network("a"), dns=True).needs_sidecar is True
        assert Switch("s", Network("a"), nat=True, uplink="eth0").needs_sidecar is True

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError):
            Switch("", Network("a"))


class TestNetworkAddressing:
    def test_bare_switch(self) -> None:
        sw = Switch("s", Network("a"), cidr="172.31.0.0/24")
        addr = NetworkAddressing.from_switch(sw)
        assert addr.cidr == "172.31.0.0/24"
        assert addr.prefix_len == 24
        assert addr.dhcp is False
        assert addr.gateway is None
        assert addr.dns_server is None

    def test_dns_on_sets_dns_server(self) -> None:
        sw = Switch("s", Network("a"), cidr="172.31.0.0/24", dns=True)
        addr = NetworkAddressing.from_switch(sw)
        assert addr.dns_server == "172.31.0.1"

    def test_nat_on_sets_gateway(self) -> None:
        sw = Switch("s", Network("a"), cidr="10.0.0.0/24", nat=True, uplink="eth0")
        addr = NetworkAddressing.from_switch(sw)
        assert addr.gateway == "10.0.0.1"

    def test_dhcp_on_flag(self) -> None:
        sw = Switch("s", Network("a"), dhcp=True)
        addr = NetworkAddressing.from_switch(sw)
        assert addr.dhcp is True

    def test_frozen(self) -> None:
        addr = NetworkAddressing.from_switch(Switch("s", Network("a")))
        with pytest.raises(dataclasses.FrozenInstanceError):
            addr.cidr = "10.0.0.0/24"  # type: ignore[misc]


def _vm(name: str, *nics: NetworkIface) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive("p1", 8), *nics],
        ),
        builder=CloudInitBuilder(base=CacheEntry("base")),
        communicator=SSHCommunicator("root"),
    )


class TestValidateAddressing:
    def test_dhcp_clean(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", dhcp=True)
        vms = [_vm("v1", NetworkIface("netA", addr=DHCPAddr()))]
        validate_addressing([sw], vms)

    def test_static_clean(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.100")))]
        validate_addressing([sw], vms)

    def test_ipv4_out_of_cidr(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("10.0.0.5")))]
        with pytest.raises(ValueError, match="not in subnet"):
            validate_addressing([sw], vms)

    def test_ipv4_is_network_address(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.0")))]
        with pytest.raises(ValueError, match="network address"):
            validate_addressing([sw], vms)

    def test_ipv4_is_broadcast(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.255")))]
        with pytest.raises(ValueError, match="broadcast"):
            validate_addressing([sw], vms)

    def test_ipv4_collides_with_sidecar(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", dhcp=True)
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.1")))]
        with pytest.raises(ValueError, match="sidecar"):
            validate_addressing([sw], vms)

    def test_ipv4_collides_with_mgmt(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", mgmt=True)
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.2")))]
        with pytest.raises(ValueError, match="mgmt"):
            validate_addressing([sw], vms)

    def test_ipv4_in_dhcp_pool(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", dhcp=True)
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.50")))]
        with pytest.raises(ValueError, match="DHCP pool"):
            validate_addressing([sw], vms)

    def test_dhcp_pool_hint_tracks_user_static_consts(self) -> None:
        # NET-1: the "pick something in X-Y" hint must be derived from
        # USER_STATIC_LO/HI, not hardcoded — those constants are the single
        # source of truth the hint exists to advertise.
        from testrange.networks._addressing_consts import USER_STATIC_HI, USER_STATIC_LO

        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", dhcp=True)
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.50")))]
        with pytest.raises(ValueError) as ei:
            validate_addressing([sw], vms)
        msg = str(ei.value)
        assert f"172.31.0.{USER_STATIC_LO}-172.31.0.{USER_STATIC_HI}" in msg

    def test_ipv4_in_dhcp_pool_ok_when_dhcp_off(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.50")))]
        validate_addressing([sw], vms)

    def test_unknown_network(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netB", addr=StaticAddr("172.31.0.100")))]
        with pytest.raises(ValueError, match="unknown network"):
            validate_addressing([sw], vms)

    def test_duplicate_ipv4_same_network(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [
            _vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.100"))),
            _vm("v2", NetworkIface("netA", addr=StaticAddr("172.31.0.100"))),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            validate_addressing([sw], vms)

    def test_dhcp_off_nic_without_static_is_allowed(self) -> None:
        # No DHCP and no static IP is fine: the NIC's behavior is the guest
        # OS's call, not the plan validator's. Must not raise.
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24")
        vms = [_vm("v1", NetworkIface("netA"))]
        validate_addressing([sw], vms)

    def test_accumulates_multiple_problems(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", dhcp=True)
        vms = [
            _vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.1"))),
            _vm("v2", NetworkIface("netA", addr=StaticAddr("10.0.0.5"))),
        ]
        with pytest.raises(ValueError) as ei:
            validate_addressing([sw], vms)
        msg = str(ei.value)
        assert "sidecar" in msg
        assert "not in subnet" in msg

    def test_mixed_nics_one_static_one_dhcp(self) -> None:
        sw_a = Switch("swA", Network("netA"), cidr="172.31.0.0/24")
        sw_b = Switch("swB", Network("netB"), cidr="10.10.10.0/24", dhcp=True)
        vms = [
            _vm(
                "v1",
                NetworkIface("netA", addr=StaticAddr("172.31.0.100")),
                NetworkIface("netB", addr=DHCPAddr()),
            ),
        ]
        validate_addressing([sw_a, sw_b], vms)
