"""Transient build-phase network synthesis.

The build Switch is resolved from the Hypervisor's user-declared ``build_switch``
(ADR-0016) by :func:`resolve_build_switch`: ``None`` => an isolated DHCP+DNS
network with no egress; a plain ``Switch`` is honored as declared, identical to a
run-phase switch. The build subnet must not collide with any user-declared
Switch, and any ``uplink`` it names must be mapped by the bound profile; the
driver's preflight validates both.
"""

from __future__ import annotations

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.handles import NetworkHandle, PoolHandle
from testrange.networks.base import (
    Network,
    Sidecar,
    Switch,
)
from testrange.networks.sidecar import sidecar_nic_specs
from testrange.vms.spec import VMSpec

BUILD_CIDR = "10.97.99.0/24"
BUILD_NETWORK_NAME = "build"
BUILD_SWITCH_NAME = "__build"


def resolve_build_switch(declared: Switch | None) -> Switch:
    """Fold a user-declared build switch into the concrete Switch the build
    phase brings up (ADR-0016). Egress is out-of-band — a build switch is just
    an ordinary :class:`Switch`, realized exactly like a run-phase one.

    - ``None`` — the default isolated build switch: DHCP+DNS, **no** uplink and
      so no internet egress (the deliberate "no build_switch => no egress" rule;
      a build needing apt/pip declares its own ``Switch``).
    - ``Switch`` — honored exactly as declared (the sidecar may even be ``None``
      for a builder that carries its own static L3). A NAT egress build switch is
      ``Switch(uplink="<named>", sidecar=Sidecar(dhcp=True, dns=True, nat=True))``;
      the driver resolves the uplink name and attaches to the out-of-band iface.
    """
    if declared is None:
        return _default_build_switch()
    return declared


def _default_build_switch() -> Switch:
    return Switch(
        BUILD_SWITCH_NAME,
        Network(BUILD_NETWORK_NAME),
        cidr=BUILD_CIDR,
        sidecar=Sidecar(dhcp=True, dns=True),
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
    # the upstream LAN — both are run-phase address modes now. The handles are
    # minted directly: synthesized internal specs resolve against the run
    # ledgers, not a user Hypervisor's registries.
    nics = [
        NetworkIface(
            NetworkHandle(name, switch=switch.name),
            addr=StaticAddr(ip) if ip is not None else DHCPAddr(),
        )
        for (name, ip) in nic_specs
    ]
    return VMSpec(
        name=f"__sidecar_{switch.name}",
        devices=[CPU(1), Memory(256), OSDrive(PoolHandle(pool_name), 2), *nics],
    )


__all__ = [
    "BUILD_CIDR",
    "BUILD_NETWORK_NAME",
    "BUILD_SWITCH_NAME",
    "_sidecar_spec",
    "resolve_build_switch",
]
