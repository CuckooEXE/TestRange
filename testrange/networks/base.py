"""Network and Switch — Plan-level network declarations on a Hypervisor.

A Switch is an L2 broadcast domain: Networks on the same Switch share L2,
different Switches do not communicate.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Network:
    """An L3 subnet on a Switch."""

    name: str
    cidr: str
    dhcp: bool = True
    dns: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Network.name must be a non-empty string")
        try:
            ipaddress.ip_network(self.cidr, strict=False)
        except ValueError as e:
            raise ValueError(f"Network.cidr is not a valid CIDR: {self.cidr!r}") from e

    @property
    def network(self) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        """Parsed ip_network object."""
        return ipaddress.ip_network(self.cidr, strict=False)

    @property
    def gateway(self) -> str:
        """Gateway address: ``network_address + 1``.

        Convention used by drivers that render a managed bridge for the
        subnet: the first usable address is the bridge's IP and the guests'
        default route.
        """
        return str(self.network.network_address + 1)


@dataclass(frozen=True)
class NetworkAddressing:
    """Per-network addressing facts a builder needs to render a guest netplan.

    Lives here (not in builders) because it is hypervisor-agnostic and
    derived purely from :class:`Network`. The orchestrator brokers: it
    builds a ``Mapping[network_name, NetworkAddressing]`` from
    ``hypervisor.all_networks`` and hands it to the builder so the builder
    never has to know about hypervisor types.
    """

    cidr: str
    prefix_len: int
    gateway: str
    dhcp: bool

    @classmethod
    def from_network(cls, net: Network) -> NetworkAddressing:
        return cls(
            cidr=net.cidr,
            prefix_len=net.network.prefixlen,
            gateway=net.gateway,
            dhcp=net.dhcp,
        )


@dataclass(frozen=True)
class Switch:
    """An L2 broadcast domain. Holds one or more Networks (port groups)."""

    name: str
    networks: tuple[Network, ...] = field(default_factory=tuple)
    mgmt: bool = False
    internet: bool = True

    def __init__(
        self,
        name: str,
        *networks: Network,
        mgmt: bool = False,
        internet: bool = True,
    ) -> None:
        """Construct: ``Switch("name", Network(...), Network(...), mgmt=False, internet=True)``.

        Networks are positional-variadic for terser plan syntax.
        """
        # Frozen-dataclass dance: bypass __setattr__ via object.__setattr__.
        if not isinstance(name, str) or not name:
            raise ValueError("Switch.name must be a non-empty string")
        for n in networks:
            if not isinstance(n, Network):
                raise TypeError(f"Switch members must be Network, got {type(n).__name__}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "networks", tuple(networks))
        object.__setattr__(self, "mgmt", bool(mgmt))
        object.__setattr__(self, "internet", bool(internet))
