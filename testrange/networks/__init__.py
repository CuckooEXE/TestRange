"""Hypervisor-neutral virtual network abstractions.

Two layers, mirroring the standard L2-virtualisation model:

* :class:`AbstractSwitch` / :class:`Switch` — an L2 switch (or its
  backend equivalent: PVE SDN zone, future VMware vSwitch).  Hosts
  one or more virtual networks; can carry physical-NIC uplinks.
* :class:`AbstractVirtualNetwork` — a network VMs attach to.
  Optionally bound to a switch via the constructor's ``switch=``
  parameter.

Concrete classes live under :mod:`testrange.backends`.
"""

from testrange.networks.base import AbstractSwitch, AbstractVirtualNetwork
from testrange.networks.generic import Switch

__all__ = [
    "AbstractSwitch",
    "AbstractVirtualNetwork",
    "Switch",
]
