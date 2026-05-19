"""Transient install-phase network synthesis.

The install Switch is a sidecar-served DHCP+DNS+NAT network so install VMs can
reach the internet for apt/pip. Uplink is provided by the Hypervisor
(`install_uplink="..."`) and bound into the synthesized switch at install-phase
entry. Subnet must not collide with any user-declared Switch; the driver's
preflight validates that.
"""

from __future__ import annotations

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.networks.base import Network, Switch
from testrange.networks.sidecar import sidecar_nic_specs
from testrange.vms.spec import VMSpec

INSTALL_CIDR = "10.97.99.0/24"
INSTALL_NETWORK_NAME = "install"
INSTALL_SWITCH_NAME = "__install"


def _install_switch(uplink: str | None) -> Switch:
    if uplink is None:
        return Switch(
            INSTALL_SWITCH_NAME,
            Network(INSTALL_NETWORK_NAME),
            cidr=INSTALL_CIDR,
            dhcp=True,
            dns=True,
        )
    return Switch(
        INSTALL_SWITCH_NAME,
        Network(INSTALL_NETWORK_NAME),
        cidr=INSTALL_CIDR,
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
    nics = [LibvirtNetworkIface(name, ipv4=ip) for (name, ip) in nic_specs]
    return VMSpec(
        name=f"__sidecar_{switch.name}",
        devices=[CPU(1), Memory(256), OSDrive(pool_name, 2), *nics],
    )


__all__ = [
    "INSTALL_CIDR",
    "INSTALL_NETWORK_NAME",
    "INSTALL_SWITCH_NAME",
    "_install_switch",
    "_sidecar_spec",
]
