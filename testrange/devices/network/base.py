"""Network interface (NIC) attached to a VM."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class NetworkIface(Device):
    """Generic NIC.

    ``network`` references a :class:`~testrange.networks.Network` by name
    (declared on the Hypervisor).

    ``ipv4`` pins the NIC to a static address. ``None`` (the default) means
    DHCP. Plan-wide validation (CIDR membership, gateway collision,
    DHCP-pool collision, duplicates across VMs) lives in
    :mod:`testrange.networks.validate` and runs at Hypervisor construction.
    """

    network: str
    ipv4: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.network, str) or not self.network:
            raise ValueError("NetworkIface.network must be a non-empty string")
        if self.ipv4 is not None:
            try:
                ipaddress.IPv4Address(self.ipv4)
            except (ipaddress.AddressValueError, ValueError) as e:
                raise ValueError(
                    f"NetworkIface.ipv4 is not a valid IPv4 address: {self.ipv4!r}"
                ) from e
