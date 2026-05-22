"""Transient build-phase network synthesis.

The build Switch is a sidecar-served DHCP+DNS+NAT network so build VMs can
reach the internet for apt/pip. Uplink is provided by the Hypervisor
(`build_uplink="..."`) and bound into the synthesized switch at build-phase
entry. Subnet must not collide with any user-declared Switch; the driver's
preflight validates that.
"""

from __future__ import annotations

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks.base import Network, Switch
from testrange.networks.sidecar import sidecar_nic_specs
from testrange.vms.spec import VMSpec

BUILD_CIDR = "10.97.99.0/24"
BUILD_NETWORK_NAME = "build"
BUILD_SWITCH_NAME = "__build"


def _build_switch(uplink: str | None) -> Switch:
    if uplink is None:
        return Switch(
            BUILD_SWITCH_NAME,
            Network(BUILD_NETWORK_NAME),
            cidr=BUILD_CIDR,
            dhcp=True,
            dns=True,
        )
    return Switch(
        BUILD_SWITCH_NAME,
        Network(BUILD_NETWORK_NAME),
        cidr=BUILD_CIDR,
        uplink=uplink,
        dhcp=True,
        dns=True,
        nat=True,
    )


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
    "BUILD_NETWORK_NAME",
    "BUILD_SWITCH_NAME",
    "_build_switch",
    "_sidecar_spec",
]
