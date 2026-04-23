"""Hypervisor-neutral virtual network abstractions.

Concrete network classes live under :mod:`testrange.backends`.
"""

from testrange.networks.base import AbstractVirtualNetwork

__all__ = [
    "AbstractVirtualNetwork",
]
