"""Libvirt-specific NIC: exposes the ``driver=`` model knob."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.network.base import NetworkIface


@dataclass(frozen=True)
class LibvirtNetworkIface(NetworkIface):
    """NIC with libvirt-specific knobs.

    Fields beyond NetworkIface:
      driver: model name (``virtio``, ``e1000``, etc.). Defaults to ``virtio``.
    """

    driver: str = "virtio"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.driver:
            raise ValueError("LibvirtNetworkIface.driver must be a non-empty string")
