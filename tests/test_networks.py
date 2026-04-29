"""Unit tests for :mod:`testrange.networks`."""

from __future__ import annotations

import ipaddress
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from testrange.backends.libvirt.network import VirtualNetwork, _mac_for_vm_network
from testrange.exceptions import NetworkError
from testrange.networks.base import AbstractVirtualNetwork


class TestMacGeneration:
    def test_qemu_oui_prefix(self) -> None:
        mac = _mac_for_vm_network("web01", "NetA")
        assert mac.startswith("52:54:00:")

    def test_mac_format(self) -> None:
        mac = _mac_for_vm_network("web01", "NetA")
        parts = mac.split(":")
        assert len(parts) == 6
        assert all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts)

    def test_deterministic(self) -> None:
        assert _mac_for_vm_network("web01", "NetA") == _mac_for_vm_network("web01", "NetA")

    def test_different_vm_different_mac(self) -> None:
        assert _mac_for_vm_network("web01", "NetA") != _mac_for_vm_network("web02", "NetA")

    def test_different_network_different_mac(self) -> None:
        assert _mac_for_vm_network("web01", "NetA") != _mac_for_vm_network("web01", "NetB")


class TestSubnetMath:
    @pytest.fixture
    def net(self) -> VirtualNetwork:
        return VirtualNetwork("TestNet", "10.0.50.0/24", dhcp=True, dns=True)

    def test_gateway_is_first_host(self, net: VirtualNetwork) -> None:
        assert net.gateway_ip == "10.0.50.1"

    def test_netmask(self, net: VirtualNetwork) -> None:
        assert net.netmask == "255.255.255.0"

    def test_prefix_len(self, net: VirtualNetwork) -> None:
        assert net.prefix_len == 24

    def test_dhcp_range_skips_static_block(self, net: VirtualNetwork) -> None:
        # Gateway is .1; static block is .2–.9; DHCP starts at .10
        assert net.dhcp_range_start == "10.0.50.10"

    def test_dhcp_range_end_is_penultimate(self, net: VirtualNetwork) -> None:
        assert net.dhcp_range_end == "10.0.50.254"

    def test_static_ip_for_index_zero(self, net: VirtualNetwork) -> None:
        # Gateway skipped; index 0 gets .2
        assert net.static_ip_for_index(0) == "10.0.50.2"

    def test_static_ip_for_index_grows(self, net: VirtualNetwork) -> None:
        assert net.static_ip_for_index(5) == "10.0.50.7"

    def test_static_ip_index_out_of_range(self, net: VirtualNetwork) -> None:
        with pytest.raises(ValueError):
            net.static_ip_for_index(10_000)

    def test_tiny_subnet_dhcp_raises(self) -> None:
        # ``/30`` (2 usable hosts) can't honour the static-block
        # reservation that ``static_ip_for_index`` hands out at
        # ``.2``-``.9``.  Earlier ``dhcp_range_start`` fell back to
        # ``.2`` here, overlapping the static pool — a registered
        # MAC and a DHCP lease could end up sharing an IP.  Now
        # raises so the misconfiguration is loud.
        tiny = VirtualNetwork("T", "192.168.99.0/30")
        with pytest.raises(ValueError, match=r"at least 10|/28 or larger"):
            _ = tiny.dhcp_range_start


class TestBackendNameAndBridge:
    def test_backend_name_requires_bind_run(self) -> None:
        net = VirtualNetwork("NetA", "10.0.0.0/24")
        with pytest.raises(RuntimeError):
            net.backend_name()

    def test_backend_name_format(self) -> None:
        net = VirtualNetwork("NetA", "10.0.0.0/24")
        net.bind_run("12345678-abcd-efab-cdef-1234567890ab")
        name = net.backend_name()
        assert name.startswith("tr-")
        assert len(name) <= 15

    def test_bridge_name_truncated_to_15(self) -> None:
        net = VirtualNetwork("VeryLongNetworkName", "10.0.0.0/24")
        net.bind_run("abcd-efgh-ijkl-mnop")
        assert len(net.bridge_name()) <= 15

    def test_backend_name_lowercases_prefix(self) -> None:
        net = VirtualNetwork("SHOUTY", "10.0.0.0/24")
        net.bind_run("abcd1234")
        assert net.backend_name().islower()


class TestVmRegistration:
    def test_register_vm_returns_matching_mac(self) -> None:
        net = VirtualNetwork("NetA", "10.0.0.0/24")
        mac = net.register_vm("web01", "10.0.0.5")
        assert mac == _mac_for_vm_network("web01", "NetA")

    def test_register_vm_appends_entry(self) -> None:
        net = VirtualNetwork("NetA", "10.0.0.0/24")
        net.register_vm("web01", "10.0.0.5")
        net.register_vm("web02", "10.0.0.6")
        assert len(net._vm_entries) == 2


class TestXmlGeneration:
    @pytest.fixture
    def net(self) -> VirtualNetwork:
        n = VirtualNetwork("NetA", "10.0.50.0/24", internet=True, dns=True)
        n.bind_run("deadbeef-0000-0000-0000-000000000000")
        n.register_vm("web01", "10.0.50.5")
        n.register_vm("db01", "10.0.50.6")
        return n

    def test_xml_parses(self, net: VirtualNetwork) -> None:
        ET.fromstring(net.to_xml())

    def test_xml_contains_network_name(self, net: VirtualNetwork) -> None:
        root = ET.fromstring(net.to_xml())
        name_el = root.find("name")
        assert name_el is not None
        assert name_el.text == net.backend_name()

    def test_nat_forward_when_internet(self, net: VirtualNetwork) -> None:
        root = ET.fromstring(net.to_xml())
        forward = root.find("forward")
        assert forward is not None
        assert forward.attrib.get("mode") == "nat"

    def test_no_forward_when_isolated(self) -> None:
        n = VirtualNetwork("Offline", "10.0.99.0/24", internet=False, dns=False)
        n.bind_run("abcdef12")
        root = ET.fromstring(n.to_xml())
        assert root.find("forward") is None

    def test_dhcp_entries_for_registered_vms(self, net: VirtualNetwork) -> None:
        root = ET.fromstring(net.to_xml())
        hosts = root.findall(".//dhcp/host")
        assert len(hosts) == 2
        names = {h.attrib["name"] for h in hosts}
        assert names == {"web01", "db01"}

    def test_dns_entries_are_fqdn_only(self, net: VirtualNetwork) -> None:
        # Only ``<vm>.<network>`` is registered; the bare hostname is
        # intentionally absent so every cross-VM lookup is explicit about
        # which network it's resolving against.
        root = ET.fromstring(net.to_xml())
        hostnames = [el.text for el in root.findall(".//dns/host/hostname")]
        assert "web01.NetA" in hostnames
        assert "db01.NetA" in hostnames
        assert "web01" not in hostnames
        assert "db01" not in hostnames

    def test_no_dhcp_section_when_disabled(self) -> None:
        n = VirtualNetwork("N", "10.0.0.0/24", dhcp=False)
        n.bind_run("deadbeef")
        root = ET.fromstring(n.to_xml())
        assert root.find(".//dhcp") is None

    def test_no_dns_section_when_no_vms(self) -> None:
        n = VirtualNetwork("N", "10.0.0.0/24", dns=True)
        n.bind_run("deadbeef")
        # No register_vm calls
        root = ET.fromstring(n.to_xml())
        assert root.find(".//dns") is None


class TestLifecycle:
    def test_start_wraps_libvirt_error(self) -> None:
        import libvirt  # noqa: F401 — stubbed by conftest if absent

        net = VirtualNetwork("N", "10.0.0.0/24")
        net.bind_run("deadbeef")
        # After the ABC refactor, network start/stop accept the
        # orchestrator as ``context`` and pull the libvirt conn off
        # its ``_conn`` attribute.
        ctx = MagicMock()
        ctx._conn.networkDefineXML.side_effect = libvirt.libvirtError(
            "defineXML failed"
        )
        with pytest.raises(NetworkError):
            net.start(ctx)

    def test_stop_is_idempotent_when_never_started(self) -> None:
        net = VirtualNetwork("N", "10.0.0.0/24")
        net.bind_run("deadbeef")
        ctx = MagicMock()
        # Should not raise even though network was never started
        net.stop(ctx)


class TestAbstractVirtualNetwork:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            AbstractVirtualNetwork("N", "10.0.0.0/24")  # type: ignore[abstract]

    def test_parses_subnet_into_ipv4network(self) -> None:
        n = VirtualNetwork("N", "192.168.1.0/24")
        assert isinstance(n._network, ipaddress.IPv4Network)
