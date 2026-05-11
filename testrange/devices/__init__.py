"""Device types attached to a VMSpec.

Each device kind lives in its own subpackage with ``base.py`` for the
generic concrete and per-driver files for driver-specific variants.
"""

from __future__ import annotations

from testrange.devices.base import Device
from testrange.devices.cpu.base import CPU
from testrange.devices.disk.base import HardDrive, OSDrive
from testrange.devices.memory.base import Memory
from testrange.devices.network.base import NetworkIface
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.devices.pool.base import StoragePool

__all__ = [
    "CPU",
    "Device",
    "HardDrive",
    "LibvirtNetworkIface",
    "Memory",
    "NetworkIface",
    "OSDrive",
    "StoragePool",
]
