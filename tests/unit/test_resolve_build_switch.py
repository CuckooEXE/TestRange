"""``resolve_build_switch`` — the build-switch synthesis seam (NET-11 / ADR-0014).

Folds a user-declared build switch (``Switch | ManagedBuildSwitch | None``) into
the concrete ``Switch`` the build phase brings up, plus an optional
:class:`ManagedEgress` carrier telling the driver to manufacture + fence the
egress segment. Three cases:

- ``None``      — the default isolated build switch (DHCP+DNS, **no** egress).
- ``Switch``    — honored as declared (BYO; sidecar may even be ``None``).
- ``ManagedBuildSwitch`` — the two-segment managed shape + a ``ManagedEgress``.
"""

from __future__ import annotations

import ipaddress

from testrange.devices.network import StaticAddr
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.orchestrator.build import (
    BUILD_CIDR,
    BUILD_EGRESS_CIDR,
    BUILD_NETWORK_NAME,
    BUILD_SWITCH_NAME,
    MANAGED_EGRESS_DNS,
    resolve_build_switch,
)


class TestResolveNone:
    def test_default_is_isolated_dhcp_dns_no_egress(self) -> None:
        # D1 (ADR-0014): no declared build switch => no internet egress. The
        # default is the DHCP+DNS internal switch, no uplink, no NAT.
        switch, egress = resolve_build_switch(None)
        assert egress is None
        assert switch.name == BUILD_SWITCH_NAME
        assert switch.cidr == BUILD_CIDR
        assert switch.uplink is None
        assert switch.networks[0].name == BUILD_NETWORK_NAME
        assert switch.sidecar is not None
        assert (switch.sidecar.dhcp, switch.sidecar.dns, switch.sidecar.nat) == (True, True, False)


class TestResolvePlainSwitch:
    def test_passthrough_identity(self) -> None:
        declared = Switch(
            "custom",
            Network("n"),
            cidr="10.5.0.0/24",
            uplink="eth0",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        )
        switch, egress = resolve_build_switch(declared)
        assert switch is declared  # honored exactly as declared
        assert egress is None

    def test_sidecarless_switch_honored(self) -> None:
        # A builder that carries its own static L3 (BUILD-1/BUILD-2) is allowed
        # a bare build switch; resolve must not synthesize a sidecar onto it.
        declared = Switch("custom", Network("n"), cidr="10.5.0.0/24")
        switch, egress = resolve_build_switch(declared)
        assert switch is declared
        assert switch.sidecar is None
        assert egress is None


class TestResolveManaged:
    def test_two_segment_shape(self) -> None:
        switch, _ = resolve_build_switch(ManagedBuildSwitch(uplink="vmbr9"))

        # Switch (internal) segment: ordinary sidecar'd switch, default build cidr.
        assert switch.name == BUILD_SWITCH_NAME
        assert switch.networks[0].name == BUILD_NETWORK_NAME
        assert switch.cidr == BUILD_CIDR
        assert switch.uplink == "vmbr9"
        assert switch.sidecar is not None
        assert (switch.sidecar.dhcp, switch.sidecar.dns, switch.sidecar.nat) == (True, True, True)

    def test_sidecar_eth1_static_in_egress_subnet(self) -> None:
        switch, egress = resolve_build_switch(ManagedBuildSwitch(uplink="vmbr9"))
        assert egress is not None
        assert egress.egress_cidr == BUILD_EGRESS_CIDR

        egress_net = ipaddress.ip_network(BUILD_EGRESS_CIDR)
        gw = str(egress_net.network_address + 1)  # .1 = backend SNAT gateway
        eth1 = str(egress_net.network_address + 2)  # .2 = sidecar eth1

        addr = switch.sidecar.addr  # type: ignore[union-attr]
        assert isinstance(addr, StaticAddr)
        assert addr.host == eth1
        assert addr.gw == gw
        assert addr.dns == MANAGED_EGRESS_DNS  # upstream resolver for the sidecar's dnsmasq

    def test_explicit_switch_cidr_respected(self) -> None:
        switch, _ = resolve_build_switch(ManagedBuildSwitch(uplink="vmbr9", cidr="10.50.0.0/24"))
        assert switch.cidr == "10.50.0.0/24"

    def test_egress_subnet_disjoint_from_switch_subnet(self) -> None:
        # The two segments must not overlap (the sidecar bridges between them).
        switch, egress = resolve_build_switch(ManagedBuildSwitch(uplink="vmbr9"))
        assert egress is not None
        assert not ipaddress.ip_network(switch.cidr).overlaps(
            ipaddress.ip_network(egress.egress_cidr)
        )
