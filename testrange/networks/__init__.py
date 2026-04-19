"""Hypervisor-neutral virtual network abstractions.

Concrete network classes live under the backend packages — the
default libvirt network is at
:class:`testrange.backends.libvirt.VirtualNetwork`.
"""

from testrange.networks.base import AbstractVirtualNetwork

__all__ = [
    "AbstractVirtualNetwork",
]
