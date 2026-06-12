"""Disk devices: OSDrive (exactly one per VMSpec) and HardDrive (data disks)."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device
from testrange.handles import PoolHandle


@dataclass(frozen=True)
class _Disk(Device):
    """Shared base for OSDrive and HardDrive.

    ``pool`` is a :class:`~testrange.handles.PoolHandle` — the typed reference
    returned by ``hyp.add_pool(...)`` / ``hyp.pools["name"]`` — never a bare
    string, so a disk on an undeclared pool cannot be expressed (ADR-0030).
    """

    pool: PoolHandle
    size_gb: int

    def __post_init__(self) -> None:
        # User-facing trust boundary: mypy enforces the handle type for typed
        # callers; this catches a bare string (or another handle kind) passed
        # dynamically, before it is stored as a silently-wrong reference.
        if not isinstance(self.pool, PoolHandle):
            raise TypeError(
                f"{type(self).__name__}.pool must be a PoolHandle from "
                f"hyp.add_pool(...) or hyp.pools['name'], got {type(self.pool).__name__}"
            )
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
