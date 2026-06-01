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

# Platform firmware the VM boots under. ``bios`` is SeaBIOS (the cross-backend
# default cloud images expect); ``uefi`` is OVMF — the firmware the installer
# media was validated under (e.g. booting the PVE installer via its x86_64-efi
# GRUB path rather than the BIOS El-Torito one). Modeled as a validated str —
# matching the project's other hardware knobs (e.g. ``ProxmoxHardDrive.bus``)
# rather than an enum. Firmware is a whole-VM property, NOT a builder one: a UEFI
# install produces a disk that panics under SeaBIOS, so the *same* firmware must
# be reproduced at run-phase create — hence it lives on the spec, which both
# phases read (BUILD-1b).
FIRMWARES = frozenset({"bios", "uefi"})


@dataclass(frozen=True)
class VMSpec:
    """Hardware-only VM spec. Validates singleton-device constraints."""

    name: str
    devices: tuple[Device, ...] = field(default_factory=tuple)
    firmware: str = "bios"

    def __init__(self, *, name: str, devices: Sequence[Device], firmware: str = "bios") -> None:
        # Backend-agnostic check only; name-charset rules are enforced at the
        # Hypervisor boundary (validate_hypervisor_plan) and per-driver.
        if not name:
            raise ValueError("VMSpec.name must be a non-empty string")
        if firmware not in FIRMWARES:
            raise ValueError(
                f"VMSpec({name!r}).firmware must be one of {sorted(FIRMWARES)}, got {firmware!r}"
            )
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
        object.__setattr__(self, "firmware", firmware)

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
