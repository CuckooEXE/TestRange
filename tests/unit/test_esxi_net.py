"""ESXI-2: L2 fabric (vSwitch + portgroup + mgmt vmk + uplink) via fakes."""

from __future__ import annotations

import pytest

from testrange.drivers.esxi import _naming
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from tests.esxi_fakes import FakeEsxiClient


def _driver(client: FakeEsxiClient, **uplinks: str) -> ESXiDriver:
    return ESXiDriver(
        EsxiConn(host="h"),
        client=client,  # type: ignore[arg-type]
        uplinks=uplinks or {"egress": "vmnic1"},
    )


def test_isolated_switch_creates_vswitch_no_uplink() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    sw = Switch("priv", Network("priv-net"), cidr="10.20.0.0/24")
    assert d.create_switch(sw, "bn-priv") is None
    vsw = _naming.vswitch_name("bn-priv")
    assert client.host._vswitches[vsw] == [], "isolated vSwitch carries no pNIC"


def test_network_adds_portgroup_on_switch_vswitch() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    sw = Switch("priv", Network("priv-net"), cidr="10.20.0.0/24")
    d.create_switch(sw, "bn-priv")
    pg = d.create_network(Network("priv-net"), sw, "bn-net", switch_backend_name="bn-priv")
    assert pg == _naming.portgroup_name("bn-net")
    assert client.host._portgroups[pg] == _naming.vswitch_name("bn-priv")


def test_mgmt_switch_adds_vmk_at_dot2() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    sw = Switch("pub", Network("pub-a"), cidr="10.30.0.0/24", mgmt=True)
    d.create_switch(sw, "bn-pub")
    ips = [ip for (_pg, ip) in client.host._vnics.values()]
    assert "10.30.0.2" in ips


def test_uplink_nat_switch_returns_uplink_portgroup_on_shared_vswitch() -> None:
    client = FakeEsxiClient()
    d = _driver(client, egress="vmnic1")
    sw = Switch(
        "pub",
        Network("pub-a"),
        cidr="10.30.0.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
    up_pg = d.create_switch(sw, "bn-pub")
    assert up_pg == _naming.uplink_portgroup_name("bn-pub")
    up_vsw = _naming.uplink_vswitch_name("vmnic1")
    assert client.host._vswitches[up_vsw] == ["vmnic1"], "uplink vSwitch enslaves the pNIC"
    assert client.host._portgroups[up_pg] == up_vsw


def test_unmapped_uplink_raises() -> None:
    client = FakeEsxiClient()
    d = ESXiDriver(EsxiConn(host="h"), client=client, uplinks={})  # type: ignore[arg-type]
    sw = Switch(
        "pub",
        Network("pub-a"),
        cidr="10.30.0.0/24",
        uplink="egress",
        sidecar=Sidecar(nat=True),
    )
    with pytest.raises(DriverError, match="does not resolve"):
        d.create_switch(sw, "bn-pub")


def test_destroy_switch_is_clean_and_idempotent() -> None:
    client = FakeEsxiClient()
    d = _driver(client, egress="vmnic1")
    sw = Switch(
        "pub",
        Network("pub-a"),
        cidr="10.30.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(nat=True),
    )
    d.create_switch(sw, "bn-pub")
    d.create_network(Network("pub-a"), sw, "bn-net", switch_backend_name="bn-pub")
    d.destroy_switch("bn-pub")
    leftover = [n for n in client.host._vswitches if n.startswith("tr")]
    leftover += [n for n in client.host._portgroups if n.startswith("tr")]
    assert not leftover, f"teardown leaked: {leftover}"
    assert not client.host._vnics, "mgmt vmk not removed"
    # idempotent second teardown
    d.destroy_switch("bn-pub")


def test_shared_uplink_vswitch_survives_until_last_portgroup() -> None:
    client = FakeEsxiClient()
    d = _driver(client, egress="vmnic1")

    def mk(name: str) -> Switch:
        return Switch(
            name,
            Network(f"{name}-net"),
            cidr="10.30.0.0/24",
            uplink="egress",
            sidecar=Sidecar(nat=True),
        )

    d.create_switch(mk("a"), "bn-a")
    d.create_switch(mk("b"), "bn-b")
    up_vsw = _naming.uplink_vswitch_name("vmnic1")
    assert up_vsw in client.host._vswitches
    d.destroy_switch("bn-a")
    assert up_vsw in client.host._vswitches, "shared uplink vSwitch removed too early"
    d.destroy_switch("bn-b")
    assert up_vsw not in client.host._vswitches, "shared uplink vSwitch not GC'd"
