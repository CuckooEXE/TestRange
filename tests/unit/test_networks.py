"""Tests for Network / Switch / NetworkAddressing / validate_addressing."""

from __future__ import annotations

import dataclasses
import ipaddress

import pytest

from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.entry import CacheEntry
from testrange.communicators.ssh import SSHCommunicator
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.networks import Network, NetworkAddressing, Switch
from testrange.networks.validate import validate_addressing
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec


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

    def test_gateway(self) -> None:
        assert Network("netA", "172.31.0.0/24").gateway == "172.31.0.1"
        assert Network("netB", "10.10.10.0/24").gateway == "10.10.10.1"
        assert Network("netC", "192.168.5.0/28").gateway == "192.168.5.1"


class TestNetworkAddressing:
    def test_from_network_dhcp_on(self) -> None:
        n = Network("netA", "172.31.0.0/24")
        addr = NetworkAddressing.from_network(n)
        assert addr.cidr == "172.31.0.0/24"
        assert addr.prefix_len == 24
        assert addr.gateway == "172.31.0.1"
        assert addr.dhcp is True

    def test_from_network_dhcp_off(self) -> None:
        n = Network("netB", "10.0.0.0/16", dhcp=False)
        addr = NetworkAddressing.from_network(n)
        assert addr.prefix_len == 16
        assert addr.gateway == "10.0.0.1"
        assert addr.dhcp is False

    def test_frozen(self) -> None:
        addr = NetworkAddressing.from_network(Network("netA", "10.0.0.0/24"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            addr.cidr = "192.168.0.0/24"  # type: ignore[misc]


# ----------------------------------------------------------------------------
# validate_addressing
#
# Plan-level cross-VM/network validation. Single-NIC parseability is in
# NetworkIface.__post_init__ — these tests cover the cross-cutting checks
# that need the full plan in hand.
# ----------------------------------------------------------------------------


def _vm(name: str, *nics: LibvirtNetworkIface) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive("p1", 8), *nics],
        ),
        builder=CloudInitBuilder(base=CacheEntry("base")),
        communicator=SSHCommunicator("root"),
    )


class TestValidateAddressing:
    def test_pure_dhcp_is_clean(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA"))]
        validate_addressing(nets, vms)  # no raise

    def test_valid_static_is_clean(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.50"))]
        validate_addressing(nets, vms)

    def test_ipv4_out_of_cidr(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="10.0.0.5"))]
        with pytest.raises(ValueError, match="not in subnet"):
            validate_addressing(nets, vms)

    def test_ipv4_is_gateway(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.1"))]
        with pytest.raises(ValueError, match="gateway"):
            validate_addressing(nets, vms)

    def test_ipv4_is_network_address(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.0"))]
        with pytest.raises(ValueError, match="network address"):
            validate_addressing(nets, vms)

    def test_ipv4_is_broadcast(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.255"))]
        with pytest.raises(ValueError, match="broadcast"):
            validate_addressing(nets, vms)

    def test_ipv4_in_dhcp_pool(self) -> None:
        nets = [Network("netA", "172.31.0.0/24", dhcp=True)]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.150"))]
        with pytest.raises(ValueError, match="DHCP pool"):
            validate_addressing(nets, vms)

    def test_ipv4_in_dhcp_pool_ok_when_dhcp_off(self) -> None:
        nets = [Network("netA", "172.31.0.0/24", dhcp=False)]
        vms = [_vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.150"))]
        validate_addressing(nets, vms)

    def test_unknown_network(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [_vm("v1", LibvirtNetworkIface("netB", ipv4="172.31.0.50"))]
        with pytest.raises(ValueError, match="unknown network"):
            validate_addressing(nets, vms)

    def test_duplicate_ipv4_same_network(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [
            _vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.50")),
            _vm("v2", LibvirtNetworkIface("netA", ipv4="172.31.0.50")),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            validate_addressing(nets, vms)

    def test_duplicate_ipv4_different_networks_ok(self) -> None:
        nets = [
            Network("netA", "172.31.0.0/24"),
            Network("netB", "10.10.10.0/24"),
        ]
        vms = [
            _vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.50")),
            _vm("v2", LibvirtNetworkIface("netB", ipv4="172.31.0.50")),
        ]
        # 172.31.0.50 is not in netB's CIDR, so the duplicate is moot — but the
        # check should not flag a cross-network "duplicate" by IP string match.
        with pytest.raises(ValueError, match="not in subnet"):
            validate_addressing(nets, vms)

    def test_dhcp_off_nic_without_static(self) -> None:
        nets = [Network("netA", "172.31.0.0/24", dhcp=False)]
        vms = [_vm("v1", LibvirtNetworkIface("netA"))]
        with pytest.raises(ValueError, match=r"nic_no_address|would never"):
            validate_addressing(nets, vms)

    def test_accumulates_multiple_problems(self) -> None:
        nets = [Network("netA", "172.31.0.0/24")]
        vms = [
            _vm("v1", LibvirtNetworkIface("netA", ipv4="172.31.0.1")),  # gateway
            _vm("v2", LibvirtNetworkIface("netA", ipv4="10.0.0.5")),  # not in cidr
        ]
        with pytest.raises(ValueError) as ei:
            validate_addressing(nets, vms)
        # Both problems should appear in the single error message.
        msg = str(ei.value)
        assert "gateway" in msg
        assert "not in subnet" in msg

    def test_mixed_nics_one_static_one_dhcp(self) -> None:
        nets = [
            Network("netA", "172.31.0.0/24"),
            Network("netB", "10.10.10.0/24"),
        ]
        vms = [
            _vm(
                "v1",
                LibvirtNetworkIface("netA", ipv4="172.31.0.50"),
                LibvirtNetworkIface("netB"),
            ),
        ]
        validate_addressing(nets, vms)  # clean


class TestSwitch:
    def test_variadic(self) -> None:
        sw = Switch(
            "sw1",
            Network("netA", "10.0.0.0/24"),
            Network("netB", "10.0.1.0/24"),
        )
        assert sw.name == "sw1"
        assert len(sw.networks) == 2

    def test_mgmt_flag(self) -> None:
        sw = Switch("sw1", mgmt=True)
        assert sw.mgmt is True

    def test_rejects_non_network(self) -> None:
        with pytest.raises(TypeError):
            Switch("sw1", "not a network")  # type: ignore[arg-type]
