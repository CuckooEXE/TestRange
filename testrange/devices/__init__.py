"""Virtual hardware device definitions for VM configuration.

Each device kind has a sealed abstract base class plus a generic
concrete subclass that every backend accepts.  Backend-specific
device variants (e.g. :class:`testrange.backends.libvirt.LibvirtHardDrive`)
extend the abstract base directly as **siblings** of the generic
class — that's how the type system catches a backend's device being
passed to a different backend's VM.

* :class:`AbstractHardDrive` ← :class:`HardDrive`
* :class:`AbstractVCPU` ← :class:`vCPU`
* :class:`AbstractMemory` ← :class:`Memory`
* :class:`AbstractVirtualNetworkRef` ← :class:`VirtualNetworkRef`

Top-level imports (``from testrange.devices import vCPU``) continue
to work unchanged.
"""

from testrange.devices.base import AbstractDevice
from testrange.devices.hard_drive import AbstractHardDrive, HardDrive
from testrange.devices.memory import AbstractMemory, Memory
from testrange.devices.sizes import normalise_size, parse_size
from testrange.devices.vcpu import AbstractVCPU, vCPU
from testrange.devices.virtual_network_ref import (
    AbstractVirtualNetworkRef,
    VirtualNetworkRef,
)

__all__ = [
    "AbstractDevice",
    "AbstractHardDrive",
    "AbstractMemory",
    "AbstractVCPU",
    "AbstractVirtualNetworkRef",
    "vCPU",
    "Memory",
    "HardDrive",
    "VirtualNetworkRef",
    "parse_size",
    "normalise_size",
]
