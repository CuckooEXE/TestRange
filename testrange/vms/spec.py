"""VMSpec — hardware-only declaration for a VM.

Constraints:
  - exactly one CPU
  - exactly one Memory
  - exactly one OSDrive
  - any number of HardDrives
  - any number of NetworkIfaces
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from testrange.devices.base import Device
from testrange.devices.cpu.base import CPU
from testrange.devices.disk.base import HardDrive, OSDrive
from testrange.devices.memory.base import Memory
from testrange.devices.network.base import NetworkIface


@dataclass(frozen=True)
class VMSpec:
    """Hardware-only VM spec. Validates singleton-device constraints."""

    name: str
    devices: tuple[Device, ...] = field(default_factory=tuple)

    def __init__(self, *, name: str, devices: Sequence[Device]) -> None:
        # Backend-agnostic check only; name-charset rules are enforced at the
        # Hypervisor boundary (validate_hypervisor_plan) and per-driver.
        if not name:
            raise ValueError("VMSpec.name must be a non-empty string")
        devs = tuple(devices)

        cpus = sum(1 for d in devs if isinstance(d, CPU))
        mems = sum(1 for d in devs if isinstance(d, Memory))
        os_drives = sum(1 for d in devs if isinstance(d, OSDrive))

        if cpus != 1:
            raise ValueError(f"VMSpec({name!r}) must have exactly one CPU, found {cpus}")
        if mems != 1:
            raise ValueError(f"VMSpec({name!r}) must have exactly one Memory, found {mems}")
        if os_drives != 1:
            raise ValueError(f"VMSpec({name!r}) must have exactly one OSDrive, found {os_drives}")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "devices", devs)

    @property
    def cpu(self) -> CPU:
        return next(d for d in self.devices if isinstance(d, CPU))

    @property
    def memory(self) -> Memory:
        return next(d for d in self.devices if isinstance(d, Memory))

    @property
    def os_drive(self) -> OSDrive:
        return next(d for d in self.devices if isinstance(d, OSDrive))

    @property
    def data_drives(self) -> tuple[HardDrive, ...]:
        return tuple(d for d in self.devices if isinstance(d, HardDrive))

    @property
    def nics(self) -> tuple[NetworkIface, ...]:
        return tuple(d for d in self.devices if isinstance(d, NetworkIface))
