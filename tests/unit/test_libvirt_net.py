"""L2 fabric for the libvirt backend (BACKEND-1.C).

Driven against a duck-typed fake of the libvirt network API. Covers isolated
network synthesis (no forward/ip/dhcp — the sidecar owns services), the
uplink-return contract for a nat switch, shared-bridge network attachment, and
tolerant teardown.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from testrange.drivers.libvirt import _net
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch


class FakeNet:
    def __init__(self, name: str) -> None:
        self.name = name
        self.active = False
        self.persistent = True
        self.ops: list[str] = []

    def create(self) -> None:
        self.active = True
        self.ops.append("create")

    def bridgeName(self) -> str:
        return "virbr-fake"

    def isActive(self) -> bool:
        return self.active

    def isPersistent(self) -> bool:
        return self.persistent

    def destroy(self) -> None:
        self.active = False
        self.ops.append("destroy")

    def undefine(self) -> None:
        self.persistent = False
        self.ops.append("undefine")


class FakeConn:
    def __init__(self) -> None:
        self.networks: dict[str, FakeNet] = {}
        self.defined_xml: list[str] = []

    def networkDefineXML(self, xml: str) -> FakeNet:
        self.defined_xml.append(xml)
        # crude name extraction for the fake
        name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
        net = FakeNet(name)
        self.networks[name] = net
        return net


class FakeClient:
    def __init__(self) -> None:
        self.conn = FakeConn()

    @property
    def raw(self) -> FakeConn:
        return self.conn

    def lookup_network(self, name: str) -> FakeNet | None:
        return self.conn.networks.get(name)


def _any_client() -> Any:
    return FakeClient()


def _switch(
    name: str = "sw", *, uplink: str | None = None, nat: bool = False, mgmt: bool = False
) -> Switch:
    sidecar = Sidecar(dhcp=True, dns=True, nat=nat) if (nat or uplink is None) else None
    if nat:
        sidecar = Sidecar(dhcp=True, dns=True, nat=True)
    return Switch(
        name, Network("netA"), cidr="10.0.0.0/24", uplink=uplink, mgmt=mgmt, sidecar=sidecar
    )


class TestCreateSwitch:
    def test_isolated_no_forward_dhcp_or_host_ip(self) -> None:
        client: Any = FakeClient()
        ret = _net.create_switch(client, _switch(), "tr-switch-x-sw")
        assert ret is None  # no uplink => no uplink segment
        xml = client.conn.defined_xml[0]
        assert "<name>tr-switch-x-sw</name>" in xml
        # A non-mgmt Switch is fully isolated: no NAT/forward, no libvirt DHCP/DNS,
        # and NO host <ip> (mgmt=False => the host is not on the segment).
        assert "<forward" not in xml and "<dhcp" not in xml and "<ip" not in xml
        assert client.conn.networks["tr-switch-x-sw"].active

    def test_mgmt_switch_gets_host_ip_at_dot2(self) -> None:
        client: Any = FakeClient()
        _net.create_switch(client, _switch(mgmt=True), "tr-switch-x-sw")
        xml = client.conn.defined_xml[0]
        # mgmt=True puts the host's .2 adapter on the bridge; <dns enable='no'>
        # keeps libvirt from spawning a dnsmasq that would shadow the sidecar.
        assert "<ip address='10.0.0.2' prefix='24'/>" in xml
        assert "<dns enable='no'/>" in xml
        assert "<forward" not in xml and "<dhcp" not in xml

    def test_bridge_name_is_explicit_and_deterministic(self) -> None:
        # A nameless <bridge> delegates to libvirtd's virbr%d allocator, which
        # races parallel switch creation (BACKEND-16): the XML must carry a
        # deterministic per-network name, distinct across networks, within
        # IFNAMSIZ-1.
        client: Any = FakeClient()
        _net.create_switch(client, _switch(), "tr-switch-x-sw")
        _net.create_switch(client, _switch("other"), "tr-switch-x-other")
        first, second = client.conn.defined_xml[0], client.conn.defined_xml[1]
        m1 = re.search(r'<bridge name="(trb-[0-9a-f]{10})"', first)
        m2 = re.search(r'<bridge name="(trb-[0-9a-f]{10})"', second)
        assert m1 and m2, f"explicit trb- bridge name missing: {first!r}"
        assert m1.group(1) != m2.group(1), "distinct networks share a bridge name"
        assert len(m1.group(1)) <= 15
        # Re-rendering the same backend name yields the same bridge name.
        client2: Any = FakeClient()
        _net.create_switch(client2, _switch(), "tr-switch-x-sw")
        assert m1.group(1) in client2.conn.defined_xml[0]

    def test_nat_switch_returns_resolved_uplink(self) -> None:
        client: Any = FakeClient()
        sw = _switch("pub", uplink="egress", nat=True)
        ret = _net.create_switch(client, sw, "tr-switch-x-pub", resolved_uplink="tr-egress")
        assert ret == "tr-egress"

    def test_nat_switch_unmapped_uplink_raises(self) -> None:
        client: Any = FakeClient()
        sw = _switch("pub", uplink="egress", nat=True)
        with pytest.raises(DriverError, match="maps no host network"):
            _net.create_switch(client, sw, "tr-switch-x-pub", resolved_uplink=None)


class TestNetworksShareBridge:
    def test_create_network_returns_switch_network(self) -> None:
        client: Any = FakeClient()
        ret = _net.create_network(
            client,
            Network("netA"),
            _switch(),
            "tr-network-x-netA",
            switch_backend_name="tr-switch-x-sw",
        )
        assert ret == "tr-switch-x-sw"  # shares the switch's libvirt network

    def test_destroy_network_is_noop(self) -> None:
        _net.destroy_network(_any_client(), "tr-network-x-netA")  # no raise


class TestDestroySwitch:
    def test_stops_and_undefines(self) -> None:
        client: Any = FakeClient()
        _net.create_switch(client, _switch(), "tr-switch-x-sw")
        _net.destroy_switch(client, "tr-switch-x-sw")
        assert client.conn.networks["tr-switch-x-sw"].ops == ["create", "destroy", "undefine"]

    def test_absent_is_noop(self) -> None:
        _net.destroy_switch(_any_client(), "ghost")  # no raise
