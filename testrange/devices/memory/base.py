"""Memory device — size in megabytes."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class Memory(Device):
    """Generic memory spec. Exactly one Memory is allowed per VMSpec.

    ``size_mb`` is interpreted as **mebibytes** (MiB = 1024 KiB) — the unit the
    backends consume (libvirt ``<memory unit='MiB'>``, PVE ``memory``). The
    ``_mb`` suffix is historical; the value is MiB, not decimal megabytes.
    """

    size_mb: int

    def __post_init__(self) -> None:
        if self.size_mb < 1:
            raise ValueError(f"Memory.size_mb must be a positive int, got {self.size_mb!r}")
