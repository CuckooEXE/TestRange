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
from testrange.networks import (
    Network,
    NetworkAddressing,
    Sidecar,
    Switch,
)
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


class TestSidecar:
    def test_dhcp_only(self) -> None:
        sc = Sidecar(dhcp=True)
        assert sc.dhcp is True
        assert sc.dns is False
        assert sc.nat is False
        assert sc.addr is None

    def test_dns_only(self) -> None:
        assert Sidecar(dns=True).dns is True

    def test_nat_only_ok_without_uplink(self) -> None:
        # The 'nat requires uplink' rule belongs to Switch (the only object
        # seeing both the services and the L2 topology); Sidecar carries
        # services alone, so nat-only is well-formed here.
        assert Sidecar(nat=True).nat is True

    def test_all_false_raises(self) -> None:
        with pytest.raises(ValueError, match=r"at least one of dhcp/dns/nat"):
            Sidecar()

    def test_addr_ok_with_nat(self) -> None:
        addr = StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",))
        sc = Sidecar(nat=True, addr=addr)
        assert sc.addr is addr

    def test_addr_requires_nat(self) -> None:
        with pytest.raises(ValueError, match=r"addr.*nat=True"):
            Sidecar(dhcp=True, addr=StaticAddr("10.10.10.2/24"))

    def test_addr_requires_explicit_prefix(self) -> None:
        # The uplink is its own subnet (not the Switch CIDR), so the netmask
        # can't be derived — a bare address is rejected.
        with pytest.raises(ValueError, match="prefix"):
            Sidecar(nat=True, addr=StaticAddr("10.10.10.2"))

    def test_frozen(self) -> None:
        sc = Sidecar(dhcp=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sc.dhcp = False  # type: ignore[misc]


class TestSwitch:
    def test_variadic_default_cidr(self) -> None:
        sw = Switch("sw1", Network("netA"), Network("netB"))
        assert sw.name == "sw1"
        assert len(sw.networks) == 2
        assert sw.cidr == "192.168.10.0/24"
        assert sw.uplink is None
        assert sw.mgmt is False
        assert sw.sidecar is None

    def test_strict_cidr_rejects_host_form(self) -> None:
        with pytest.raises(ValueError, match=r"strict form|host address|network address"):
            Switch("sw1", Network("a"), cidr="192.168.10.1/24")

    def test_strict_cidr_accepts_network_form(self) -> None:
        sw = Switch("sw1", Network("a"), cidr="10.0.0.0/24")
        assert sw.cidr == "10.0.0.0/24"
        assert sw.network.network_address == ipaddress.IPv4Address("10.0.0.0")

    def test_prefix_longer_than_24_rejected(self) -> None:
        # H7: the .1-.254 reserved/DHCP/static layout needs a full /24 host
        # space; a /25 would overrun the subnet broadcast.
        with pytest.raises(ValueError, match=r"/24 or larger|prefix <= 24"):
            Switch("sw1", Network("a"), cidr="10.0.0.0/25")

    def test_prefix_shorter_than_24_allowed(self) -> None:
        sw = Switch("sw1", Network("a"), cidr="10.0.0.0/16")
        assert sw.network.prefixlen == 16

    def test_ipv6_rejected(self) -> None:
        with pytest.raises(ValueError, match="IPv4"):
            Switch("sw1", Network("a"), cidr="fd00::/64")

    def test_nat_sidecar_without_uplink_raises(self) -> None:
        # nat-requires-uplink is the one invariant spanning topology + services;
        # Switch.__init__ is the only object seeing both, so it enforces it.
        with pytest.raises(ValueError, match=r"nat=True.*uplink"):
            Switch("sw1", Network("a"), sidecar=Sidecar(nat=True))

    def test_nat_sidecar_with_uplink_ok(self) -> None:
        sw = Switch("sw1", Network("a"), uplink="eth0", sidecar=Sidecar(nat=True))
        assert sw.sidecar is not None and sw.sidecar.nat is True
        assert sw.uplink == "eth0"
        assert sw.needs_sidecar is True

    def test_dhcp_sidecar_needs_no_uplink(self) -> None:
        # A non-NAT sidecar (dhcp/dns only) has no uplink requirement.
        sw = Switch("sw1", Network("a"), sidecar=Sidecar(dhcp=True))
        assert sw.uplink is None
        assert sw.needs_sidecar is True

    def test_empty_uplink_rejected(self) -> None:
        with pytest.raises(ValueError, match="uplink"):
            Switch("sw1", Network("a"), uplink="")

    def test_sidecar_addr_ok_with_nat(self) -> None:
        # NET-7: static address for the sidecar's MASQUERADE uplink NIC.
        addr = StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",))
        sw = Switch("sw1", Network("a"), uplink="vmbr9", sidecar=Sidecar(nat=True, addr=addr))
        assert sw.sidecar is not None and sw.sidecar.addr is addr

    def test_pinned_addresses(self) -> None:
        sw = Switch("sw1", Network("a"), cidr="172.31.0.0/24")
        assert sw.sidecar_ip == "172.31.0.1"
        assert sw.mgmt_ip == "172.31.0.2"

    def test_needs_sidecar_flags(self) -> None:
        assert Switch("s", Network("a")).needs_sidecar is False
        assert Switch("s", Network("a"), sidecar=Sidecar(dhcp=True)).needs_sidecar is True
        assert Switch("s", Network("a"), sidecar=Sidecar(dns=True)).needs_sidecar is True
        assert (
            Switch("s", Network("a"), uplink="eth0", sidecar=Sidecar(nat=True)).needs_sidecar
            is True
        )

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
        sw = Switch("s", Network("a"), cidr="172.31.0.0/24", sidecar=Sidecar(dns=True))
        addr = NetworkAddressing.from_switch(sw)
        assert addr.dns_server == "172.31.0.1"

    def test_nat_on_sets_gateway(self) -> None:
        sw = Switch("s", Network("a"), cidr="10.0.0.0/24", uplink="eth0", sidecar=Sidecar(nat=True))
        addr = NetworkAddressing.from_switch(sw)
        assert addr.gateway == "10.0.0.1"

    def test_dhcp_on_flag(self) -> None:
        sw = Switch("s", Network("a"), sidecar=Sidecar(dhcp=True))
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
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True))
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
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True))
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.1")))]
        with pytest.raises(ValueError, match="sidecar"):
            validate_addressing([sw], vms)

    def test_ipv4_collides_with_mgmt(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", mgmt=True)
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.2")))]
        with pytest.raises(ValueError, match="mgmt"):
            validate_addressing([sw], vms)

    def test_ipv4_in_dhcp_pool(self) -> None:
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True))
        vms = [_vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.50")))]
        with pytest.raises(ValueError, match="DHCP pool"):
            validate_addressing([sw], vms)

    def test_dhcp_pool_hint_tracks_user_static_consts(self) -> None:
        # NET-1: the "pick something in X-Y" hint must be derived from
        # USER_STATIC_LO/HI, not hardcoded — those constants are the single
        # source of truth the hint exists to advertise.
        from testrange.networks._addressing_consts import USER_STATIC_HI, USER_STATIC_LO

        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True))
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

    def test_duplicate_ipv4_across_networks_of_same_switch(self) -> None:
        # H8: netA and netB share the Switch's one CIDR/L2 wire, so the same
        # static IP on each still collides on the wire — dedup is keyed by the
        # Switch, not the Network name.
        sw = Switch("sw", Network("netA"), Network("netB"), cidr="172.31.0.0/24")
        vms = [
            _vm("v1", NetworkIface("netA", addr=StaticAddr("172.31.0.100"))),
            _vm("v2", NetworkIface("netB", addr=StaticAddr("172.31.0.100"))),
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
        sw = Switch("sw", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True))
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
        sw_b = Switch("swB", Network("netB"), cidr="10.10.10.0/24", sidecar=Sidecar(dhcp=True))
        vms = [
            _vm(
                "v1",
                NetworkIface("netA", addr=StaticAddr("172.31.0.100")),
                NetworkIface("netB", addr=DHCPAddr()),
            ),
        ]
        validate_addressing([sw_a, sw_b], vms)
