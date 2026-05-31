"""Tests for orchestrator.provision helpers — NET-8 uplink-addr injection."""

from __future__ import annotations

from typing import Any

from testrange.devices.network import StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.provision import _effective_switch

_ADDR = StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",))


def _sw(**kw: Any) -> Switch:
    return Switch("pub", Network("a"), cidr="10.30.0.0/24", **kw)


class TestEffectiveSwitch:
    def test_injects_profile_addr_into_nat_sidecar(self) -> None:
        sw = _sw(uplink="egress", sidecar=Sidecar(dhcp=True, dns=True, nat=True))
        out = _effective_switch(sw, {"egress": _ADDR})
        assert out.sidecar is not None and out.sidecar.addr is _ADDR
        # the rest of the switch + sidecar services are preserved
        assert out.name == "pub" and out.uplink == "egress" and out.cidr == "10.30.0.0/24"
        assert out.sidecar.dhcp and out.sidecar.dns and out.sidecar.nat

    def test_noop_when_profile_maps_no_addr_for_the_uplink(self) -> None:
        sw = _sw(uplink="egress", sidecar=Sidecar(nat=True))
        assert _effective_switch(sw, {}) is sw

    def test_noop_when_switch_has_no_sidecar(self) -> None:
        sw = _sw()
        assert _effective_switch(sw, {"egress": _ADDR}) is sw

    def test_noop_when_sidecar_has_no_nat(self) -> None:
        sw = _sw(sidecar=Sidecar(dhcp=True))  # no nat → no uplink needed, no MASQUERADE NIC
        assert _effective_switch(sw, {"egress": _ADDR}) is sw

    def test_plan_set_addr_is_not_overridden(self) -> None:
        plan_addr = StaticAddr("10.10.10.9/24", gw="10.10.10.1")
        sw = _sw(uplink="egress", sidecar=Sidecar(nat=True, addr=plan_addr))
        out = _effective_switch(sw, {"egress": _ADDR})
        assert out is sw and out.sidecar is not None and out.sidecar.addr is plan_addr
