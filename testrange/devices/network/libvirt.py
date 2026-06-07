"""Libvirt-specific NIC variant: pick the emulated NIC ``model``.

Subclasses the portable :class:`~testrange.devices.NetworkIface` to expose the
QEMU NIC model. A plan that uses one is, by construction, pinned to the libvirt
backend.

The motivating case is a **nested ESXi guest**: ESXi ships no virtio-net driver,
so its NIC must be emulated as ``e1000e`` rather than the libvirt default
``virtio``. A plain :class:`~testrange.devices.NetworkIface` keeps virtio-net.
"""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.network.base import NetworkIface

# NIC models libvirt/QEMU can emulate. ``virtio`` (virtio-net) is the fast
# paravirtual default; ``e1000``/``e1000e``/``rtl8139`` are fully-emulated cards
# for guests with no virtio-net driver (ESXi needs ``e1000e``).
LIBVIRT_NIC_MODELS = frozenset({"virtio", "e1000", "e1000e", "rtl8139"})


@dataclass(frozen=True)
class LibvirtNetworkIface(NetworkIface):
    """A NIC emulated as a chosen libvirt/QEMU ``model`` (default virtio-net)."""

    model: str = "virtio"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.model not in LIBVIRT_NIC_MODELS:
            raise ValueError(
                f"LibvirtNetworkIface.model must be one of {sorted(LIBVIRT_NIC_MODELS)}, "
                f"got {self.model!r}"
            )


__all__ = ["LIBVIRT_NIC_MODELS", "LibvirtNetworkIface"]
