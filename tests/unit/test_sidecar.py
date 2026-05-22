"""Tests for the per-Switch sidecar renderers."""

from __future__ import annotations

from testrange.builders.cloudinit import CloudInitBuilder
from testrange.cache.entry import CacheEntry
from testrange.communicators.ssh import SSHCommunicator
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr
from testrange.devices.network import NetworkIface
from testrange.networks import Network, Switch
from testrange.networks.sidecar import (
    SIDECAR_SWITCH_NIC,
    SIDECAR_UPLINK_NIC,
    parse_dnsmasq_leases,
    render_dnsmasq_conf,
    render_nftables_ruleset,
    render_sidecar_interfaces,
    render_sysctl_conf,
    sidecar_nic_specs,
)
from testrange.vms.recipe import VMRecipe
from testrange.vms.spec import VMSpec


def _vm(name: str, *nics: NetworkIface) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive("p1", 8), *nics],
        ),
        builder=CloudInitBuilder(base=CacheEntry("base")),
        communicator=SSHCommunicator("root"),
    )


def _mac_for(_vm_name: str, idx: int) -> str:
    return f"52:54:00:aa:bb:{idx:02x}"


class TestSidecarNicSpecs:
    def test_no_nat_single_nic(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dhcp=True)
        specs = sidecar_nic_specs(sw)
        assert specs == [("a", "10.0.0.1")]

    def test_nat_adds_uplink_nic(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", nat=True, uplink="eth0")
        specs = sidecar_nic_specs(sw)
        assert specs == [("a", "10.0.0.1"), ("__uplink__sw", None)]


class TestRenderSidecarInterfaces:
    def test_basic_static_eth0(self) -> None:
        sw = Switch("sw", Network("a"), cidr="172.31.0.0/24", dhcp=True)
        cfg = render_sidecar_interfaces(sw)
        assert f"auto {SIDECAR_SWITCH_NIC}" in cfg
        assert "address 172.31.0.1" in cfg
        assert "netmask 255.255.255.0" in cfg
        assert SIDECAR_UPLINK_NIC not in cfg

    def test_nat_adds_dhcp_eth1(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", nat=True, uplink="eth0")
        cfg = render_sidecar_interfaces(sw)
        assert f"auto {SIDECAR_UPLINK_NIC}" in cfg
        assert f"iface {SIDECAR_UPLINK_NIC} inet dhcp" in cfg


class TestRenderDnsmasqConf:
    def test_bare(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24")
        conf = render_dnsmasq_conf(sw, [], _mac_for)
        assert "port=0" in conf
        assert "dhcp-range" not in conf

    def test_dhcp_only(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dhcp=True)
        conf = render_dnsmasq_conf(sw, [], _mac_for)
        assert "dhcp-range=10.0.0.10,10.0.0.99" in conf
        assert "port=0" in conf  # no DNS listener
        assert "dhcp-option=3" in conf  # gateway suppressed (no nat)
        assert "dhcp-option=6" in conf  # dns suppressed (no dns)

    def test_dhcp_plus_dns(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dhcp=True, dns=True)
        conf = render_dnsmasq_conf(sw, [], _mac_for)
        assert "port=0" not in conf
        assert "option:dns-server,10.0.0.1" in conf
        assert "domain=a,10.0.0.0/24" in conf

    def test_nat_advertises_router(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dhcp=True, nat=True, uplink="eth0")
        conf = render_dnsmasq_conf(sw, [], _mac_for)
        assert "option:router,10.0.0.1" in conf
        assert "dhcp-option=3" not in conf

    def test_static_nic_gets_host_record(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dns=True)
        vms = [_vm("v1", NetworkIface("a", addr=StaticAddr("10.0.0.100")))]
        conf = render_dnsmasq_conf(sw, vms, _mac_for)
        assert "host-record=v1.a,10.0.0.100" in conf

    def test_dhcp_nic_gets_dhcp_host(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dhcp=True)
        vms = [_vm("v1", NetworkIface("a", addr=DHCPAddr()))]
        conf = render_dnsmasq_conf(sw, vms, _mac_for)
        assert "dhcp-host=52:54:00:aa:bb:00,v1" in conf

    def test_ignores_nics_on_other_switches(self) -> None:
        sw = Switch("sw", Network("a"), cidr="10.0.0.0/24", dns=True)
        vms = [_vm("v1", NetworkIface("b", addr=StaticAddr("10.0.0.50")))]
        conf = render_dnsmasq_conf(sw, vms, _mac_for)
        assert "host-record" not in conf


class TestRenderNftablesRuleset:
    def test_no_nat_empty(self) -> None:
        sw = Switch("sw", Network("a"))
        rules = render_nftables_ruleset(sw)
        assert rules.strip() == "flush ruleset"

    def test_nat_emits_masquerade(self) -> None:
        sw = Switch("sw", Network("a"), nat=True, uplink="eth0")
        rules = render_nftables_ruleset(sw)
        assert "table ip nat" in rules
        assert f'oifname "{SIDECAR_UPLINK_NIC}" masquerade' in rules


class TestRenderSysctlConf:
    def test_no_nat_empty(self) -> None:
        assert render_sysctl_conf(Switch("sw", Network("a"))) == ""

    def test_nat_enables_forwarding(self) -> None:
        sw = Switch("sw", Network("a"), nat=True, uplink="eth0")
        assert "net.ipv4.ip_forward=1" in render_sysctl_conf(sw)


class TestParseDnsmasqLeases:
    def test_basic(self) -> None:
        text = (
            "1716044000 52:54:00:aa:bb:01 10.0.0.100 vm1 *\n"
            "1716044000 52:54:00:aa:bb:02 10.0.0.101 vm2 *\n"
        )
        assert parse_dnsmasq_leases(text) == {
            "52:54:00:aa:bb:01": "10.0.0.100",
            "52:54:00:aa:bb:02": "10.0.0.101",
        }

    def test_empty(self) -> None:
        assert parse_dnsmasq_leases("") == {}

    def test_uppercase_mac_lowered(self) -> None:
        text = "1716044000 AA:BB:CC:DD:EE:FF 10.0.0.5 host *\n"
        assert parse_dnsmasq_leases(text) == {"aa:bb:cc:dd:ee:ff": "10.0.0.5"}


def test_dhcp_pool_uses_addressing_consts_bounds() -> None:
    sw = Switch("sw", Network("a"), cidr="172.16.0.0/24", dhcp=True)
    conf = render_dnsmasq_conf(sw, [], _mac_for)
    assert "172.16.0.10,172.16.0.99" in conf
