"""Device types attached to a VMSpec.

Each device kind lives in its own subpackage. Generic device shapes are
exported here; any driver-specific variant lives under its driver-named
submodule and is imported directly from there.
"""

from __future__ import annotations

from testrange.devices.base import Device
from testrange.devices.cpu.base import CPU
from testrange.devices.disk.base import HardDrive, OSDrive
from testrange.devices.memory.base import Memory
from testrange.devices.network.base import DHCPAddr, NetworkIface, StaticAddr
from testrange.devices.pool.base import StoragePool

__all__ = [
    "CPU",
    "DHCPAddr",
    "Device",
    "HardDrive",
    "Memory",
    "NetworkIface",
    "OSDrive",
    "StaticAddr",
    "StoragePool",
]
