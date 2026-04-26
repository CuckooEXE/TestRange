"""Virtual CPU core allocation."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class AbstractVCPU(AbstractDevice):
    """Sealed base class for vCPU specifications.

    Backend-specific vCPU subclasses (with options like CPU topology,
    CPU pinning, or model overrides) live in their backend module and
    extend this directly — siblings of :class:`vCPU`, not children.
    The type system catches a backend-specific vCPU spec being passed
    to a different backend's VM.
    """

    count: int
    """Number of virtual CPU cores to allocate to the VM."""

    @property
    def device_type(self) -> str:
        return "vcpu"


class vCPU(AbstractVCPU):
    """Generic vCPU spec — accepted by every backend.

    Carries only the universal field (core count); backends pick
    sensible defaults for topology, model, and pinning.

    :param count: Number of vCPU cores to allocate.  Defaults to ``2``.

    Example::

        vCPU(4)   # allocate 4 virtual cores
    """

    def __init__(self, count: int = 2) -> None:
        if count < 1:
            raise ValueError(f"vCPU count must be >= 1, got {count}")
        self.count = count

    def __repr__(self) -> str:
        return f"vCPU({self.count!r})"


__all__ = ["AbstractVCPU", "vCPU"]
