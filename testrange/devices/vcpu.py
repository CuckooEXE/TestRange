"""Virtual CPU core allocation."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class vCPU(AbstractDevice):
    """Specifies the number of virtual CPU cores for a VM.

    :param count: Number of vCPU cores to allocate.  Defaults to ``2``.

    Example::

        vCPU(4)   # allocate 4 virtual cores
    """

    count: int
    """Number of virtual CPU cores to allocate to the VM."""

    def __init__(self, count: int = 2) -> None:
        if count < 1:
            raise ValueError(f"vCPU count must be >= 1, got {count}")
        self.count = count

    @property
    def device_type(self) -> str:
        """Return ``'vcpu'``.

        :returns: The string ``'vcpu'``.
        """
        return "vcpu"

    def __repr__(self) -> str:
        return f"vCPU({self.count!r})"


__all__ = ["vCPU"]
