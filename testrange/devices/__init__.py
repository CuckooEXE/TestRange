"""Virtual hardware device definitions for VM configuration.

One module per device type: subclass :class:`AbstractDevice` in its
own file, re-export from here.  Top-level imports
(``from testrange.devices import vCPU``) continue to work unchanged.
"""

from testrange.devices.base import AbstractDevice
from testrange.devices.hard_drive import HardDrive
from testrange.devices.memory import Memory
from testrange.devices.sizes import normalise_size, parse_size
from testrange.devices.vcpu import vCPU
from testrange.devices.virtual_network_ref import VirtualNetworkRef

__all__ = [
    "AbstractDevice",
    "vCPU",
    "Memory",
    "HardDrive",
    "VirtualNetworkRef",
    "parse_size",
    "normalise_size",
]
