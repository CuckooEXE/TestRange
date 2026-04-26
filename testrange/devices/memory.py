"""Virtual RAM allocation."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class AbstractMemory(AbstractDevice):
    """Sealed base class for memory specifications.

    Backend-specific memory subclasses (with options like ballooning,
    hugepages, or NUMA placement) live in their backend module and
    extend this directly — siblings of :class:`Memory`, not children.
    """

    gib: float
    """RAM allocation in gibibytes (GiB)."""

    @property
    def device_type(self) -> str:
        return "memory"

    @property
    def kib(self) -> int:
        """Return memory size in kibibytes (rounded to nearest integer)."""
        return round(self.gib * 1024 * 1024)


class Memory(AbstractMemory):
    """Generic memory spec — accepted by every backend.

    :param gib: Memory size in gibibytes (GiB).  Defaults to ``2``.

    Example::

        Memory(8)   # allocate 8 GiB RAM
    """

    def __init__(self, gib: float = 2.0) -> None:
        if gib <= 0:
            raise ValueError(f"Memory must be > 0 GiB, got {gib}")
        self.gib = gib

    def __repr__(self) -> str:
        return f"Memory({self.gib!r})"


__all__ = ["AbstractMemory", "Memory"]
