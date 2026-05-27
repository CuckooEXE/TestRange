"""Disk devices: OSDrive (exactly one per VMSpec) and HardDrive (data disks)."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class _Disk(Device):
    """Shared base for OSDrive and HardDrive."""

    pool: str
    size_gb: int

    def __post_init__(self) -> None:
        if not self.pool:
            raise ValueError(f"{type(self).__name__}.pool must be a non-empty string")
        if self.size_gb < 1:
            raise ValueError(
                f"{type(self).__name__}.size_gb must be a positive int, got {self.size_gb!r}"
            )


@dataclass(frozen=True)
class OSDrive(_Disk):
    """The disk the OS is installed onto. Exactly one OSDrive per VMSpec."""


@dataclass(frozen=True)
class HardDrive(_Disk):
    """A data disk. Zero or more per VMSpec."""
