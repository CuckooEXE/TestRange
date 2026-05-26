"""Transient build-phase network synthesis.

The build Switch is resolved from the Hypervisor's user-declared ``build_switch``
(ADR-0014) by :func:`resolve_build_switch`: ``None`` => an isolated DHCP+DNS
network with no egress; a plain ``Switch`` is honored as declared; a
``ManagedBuildSwitch`` is expanded into a NAT'd two-segment switch plus a
:class:`~testrange.networks.base.ManagedEgress` carrier telling the driver to
manufacture and fence the egress segment. The build subnet must not collide with
any user-declared Switch; the driver's preflight validates that.
"""

from __future__ import annotations

import ipaddress

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks.base import (
    ManagedBuildSwitch,
    ManagedEgress,
    Network,
    Sidecar,
    Switch,
)
from testrange.networks.sidecar import sidecar_nic_specs
from testrange.vms.spec import VMSpec

BUILD_CIDR = "10.97.99.0/24"
BUILD_NETWORK_NAME = "build"
BUILD_SWITCH_NAME = "__build"

# The egress segment a ManagedBuildSwitch manufactures (ADR-0014): a subnet
# distinct from BUILD_CIDR so the sidecar can bridge between the two. ``.1`` is
# the backend SNAT gateway, ``.2`` the sidecar's static eth1.
BUILD_EGRESS_CIDR = "10.97.98.0/24"
# The managed egress segment has no resolver of its own, and the sidecar's eth1
# is static (so resolv.conf stays empty — see sidecar.render_dnsmasq_conf). The
# sidecar's dnsmasq must therefore be handed an explicit upstream or build-VM
# name resolution is dead; we point it at a public resolver reachable through
# the manufactured SNAT gateway (the same resolver the manual host-NAT recipe
# used in the examples).
MANAGED_EGRESS_DNS = ("1.1.1.1",)


def resolve_build_switch(
    declared: Switch | ManagedBuildSwitch | None,
) -> tuple[Switch, ManagedEgress | None]:
    """Fold a user-declared build switch into the concrete Switch the build
    phase brings up, plus an optional :class:`ManagedEgress` carrier (ADR-0014).

    - ``None`` — the default isolated build switch: DHCP+DNS, **no** uplink and
      so no internet egress (the deliberate "no build_switch => no egress" rule;
      a build needing apt/pip declares its own ``Switch`` or ``ManagedBuildSwitch``).
    - ``Switch`` — honored exactly as declared (bring-your-own; the sidecar may
      even be ``None`` for a builder that carries its own static L3). No managed
      egress; if it sets an ``uplink``, the driver bridges to that existing NIC.
    - ``ManagedBuildSwitch`` — synthesized into the two-segment managed shape and
      paired with a :class:`ManagedEgress` instructing the driver to manufacture
      and fence the egress segment.
    """
    if declared is None:
        return _default_build_switch(), None
    if isinstance(declared, ManagedBuildSwitch):
        return _managed_build_switch(declared)
    return declared, None


def _default_build_switch() -> Switch:
    return Switch(
        BUILD_SWITCH_NAME,
        Network(BUILD_NETWORK_NAME),
        cidr=BUILD_CIDR,
        sidecar=Sidecar(dhcp=True, dns=True),
    )


def _managed_build_switch(declared: ManagedBuildSwitch) -> tuple[Switch, ManagedEgress]:
    egress_net = ipaddress.ip_network(BUILD_EGRESS_CIDR, strict=True)
    gateway = str(egress_net.network_address + 1)  # .1 — backend SNAT gateway
    sidecar_eth1 = f"{egress_net.network_address + 2}/{egress_net.prefixlen}"  # .2
    switch = Switch(
        BUILD_SWITCH_NAME,
        Network(BUILD_NETWORK_NAME),
        cidr=declared.cidr or BUILD_CIDR,
        uplink=declared.uplink,
        # eth1 is static on the manufactured egress subnet (the segment has no
        # DHCP); the gateway and upstream DNS are TestRange-assigned, not
        # leased from an upstream LAN as in the plain-uplink case.
        sidecar=Sidecar(
            dhcp=True,
            dns=True,
            nat=True,
            addr=StaticAddr(sidecar_eth1, gw=gateway, dns=MANAGED_EGRESS_DNS),
        ),
    )
    return switch, ManagedEgress(egress_cidr=BUILD_EGRESS_CIDR)


def _sidecar_spec(switch: Switch, pool_name: str) -> VMSpec:
    """Synthesize the sidecar VM's spec for one Switch.

    Always 1 vCPU + 256 MiB + 2 GiB OS disk. NICs in the order produced
    by :func:`sidecar_nic_specs`: ``eth0`` on the switch network (static
    ``.1``), and ``eth1`` on the hidden ``__uplink__<switch>`` network
    (no static IP — sidecar DHCPs from the upstream LAN) when ``nat=True``.
    """
    nic_specs = sidecar_nic_specs(switch)
    # eth0 is the static sidecar address; eth1 (uplink, when nat) DHCPs from
    # the upstream LAN — both are run-phase address modes now.
    nics = [
        NetworkIface(name, addr=StaticAddr(ip) if ip is not None else DHCPAddr())
        for (name, ip) in nic_specs
    ]
    return VMSpec(
        name=f"__sidecar_{switch.name}",
        devices=[CPU(1), Memory(256), OSDrive(pool_name, 2), *nics],
    )


__all__ = [
    "BUILD_CIDR",
    "BUILD_EGRESS_CIDR",
    "BUILD_NETWORK_NAME",
    "BUILD_SWITCH_NAME",
    "MANAGED_EGRESS_DNS",
    "_sidecar_spec",
    "resolve_build_switch",
]
