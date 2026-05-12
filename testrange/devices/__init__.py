"""Device types attached to a VMSpec.

Each device kind lives in its own subpackage. Generic device shapes are
exported here; driver-specific variants (e.g., ``LibvirtNetworkIface``) live
under their driver-named submodule and are imported directly from there.
"""

from __future__ import annotations

from testrange.devices.base import Device
from testrange.devices.cpu.base import CPU
from testrange.devices.disk.base import HardDrive, OSDrive
from testrange.devices.memory.base import Memory
from testrange.devices.network.base import NetworkIface
from testrange.devices.pool.base import StoragePool

__all__ = [
    "CPU",
    "Device",
    "HardDrive",
    "Memory",
    "NetworkIface",
    "OSDrive",
    "StoragePool",
]
