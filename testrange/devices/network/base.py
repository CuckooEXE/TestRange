"""Network interface (NIC) attached to a VM."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class NetworkIface(Device):
    """Generic NIC. ``network`` references a Network by name (declared on the Hypervisor)."""

    network: str

    def __post_init__(self) -> None:
        if not isinstance(self.network, str) or not self.network:
            raise ValueError("NetworkIface.network must be a non-empty string")
