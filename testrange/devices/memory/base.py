"""Memory device — size in megabytes."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class Memory(Device):
    """Generic memory spec. Exactly one Memory is allowed per VMSpec.

    ``size_mb`` is megabytes (1024 KiB), not mebibytes. Callers who care
    about the difference should specify the larger of the two when in
    doubt.
    """

    size_mb: int

    def __post_init__(self) -> None:
        if not isinstance(self.size_mb, int) or self.size_mb < 1:
            raise ValueError(f"Memory.size_mb must be a positive int, got {self.size_mb!r}")
