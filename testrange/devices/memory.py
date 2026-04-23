"""Virtual RAM allocation."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class Memory(AbstractDevice):
    """Specifies the RAM allocation for a VM.

    :param gib: Memory size in gibibytes (GiB).  Defaults to ``2``.

    Example::

        Memory(8)   # allocate 8 GiB RAM
    """

    gib: float
    """RAM allocation in gibibytes (GiB)."""

    def __init__(self, gib: float = 2.0) -> None:
        if gib <= 0:
            raise ValueError(f"Memory must be > 0 GiB, got {gib}")
        self.gib = gib

    @property
    def kib(self) -> int:
        """Return memory size in kibibytes.

        :returns: Memory in KiB (rounded to nearest integer).
        """
        return round(self.gib * 1024 * 1024)

    @property
    def device_type(self) -> str:
        """Return ``'memory'``.

        :returns: The string ``'memory'``.
        """
        return "memory"

    def __repr__(self) -> str:
        return f"Memory({self.gib!r})"


__all__ = ["Memory"]
