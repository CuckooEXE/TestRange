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
        if not isinstance(name, str) or not name:
            raise ValueError("VMSpec.name must be a non-empty string")
        devs = tuple(devices)

        cpus = [d for d in devs if isinstance(d, CPU)]
        mems = [d for d in devs if isinstance(d, Memory)]
        os_drives = [d for d in devs if isinstance(d, OSDrive)]

        if len(cpus) != 1:
            raise ValueError(f"VMSpec({name!r}) must have exactly one CPU, found {len(cpus)}")
        if len(mems) != 1:
            raise ValueError(f"VMSpec({name!r}) must have exactly one Memory, found {len(mems)}")
        if len(os_drives) != 1:
            raise ValueError(
                f"VMSpec({name!r}) must have exactly one OSDrive, found {len(os_drives)}"
            )
        for d in devs:
            if not isinstance(d, Device):
                raise TypeError(
                    f"VMSpec({name!r}) devices must be Device instances, got {type(d).__name__}"
                )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "devices", devs)

    @property
    def cpu(self) -> CPU:
        for d in self.devices:
            if isinstance(d, CPU):
                return d
        raise AssertionError("CPU missing — should be unreachable after __init__ validation")

    @property
    def memory(self) -> Memory:
        for d in self.devices:
            if isinstance(d, Memory):
                return d
        raise AssertionError("Memory missing — should be unreachable")

    @property
    def os_drive(self) -> OSDrive:
        for d in self.devices:
            if isinstance(d, OSDrive):
                return d
        raise AssertionError("OSDrive missing — should be unreachable")

    @property
    def data_drives(self) -> tuple[HardDrive, ...]:
        return tuple(d for d in self.devices if isinstance(d, HardDrive))

    @property
    def nics(self) -> tuple[NetworkIface, ...]:
        return tuple(d for d in self.devices if isinstance(d, NetworkIface))
