"""StoragePool — declared at the Hypervisor level, referenced by disks by name.

(Lives under ``devices/pool/`` per PLAN.md layout; conceptually a
Hypervisor-level entity, not a per-VM device.)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoragePool:
    """A storage pool declared on a Hypervisor. Disks reference it by ``name``."""

    name: str
    size_gb: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("StoragePool.name must be a non-empty string")
        if not isinstance(self.size_gb, int) or self.size_gb < 1:
            raise ValueError(
                f"StoragePool.size_gb must be a positive int, got {self.size_gb!r}"
            )
