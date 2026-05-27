"""StoragePool — declared at the Hypervisor level, referenced by disks by name.

(Lives under ``devices/`` for layout uniformity, but is conceptually a
Hypervisor-level entity rather than a per-VM device.)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoragePool:
    """A storage pool declared on a Hypervisor. Disks reference it by ``name``."""

    name: str
    size_gb: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("StoragePool.name must be a non-empty string")
        if self.size_gb < 1:
            raise ValueError(f"StoragePool.size_gb must be a positive int, got {self.size_gb!r}")
