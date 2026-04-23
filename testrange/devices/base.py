"""Abstract base class for virtual hardware devices."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AbstractDevice(ABC):
    """Base class for all virtual hardware devices attached to a VM.

    Subclass this to implement custom device types.  Devices are passed as a
    list to the ``devices=`` parameter of :class:`~testrange.vms.base.AbstractVM`.

    Example::

        class VirtualTPM(AbstractDevice):
            @property
            def device_type(self) -> str:
                return "vtpm"
    """

    @property
    @abstractmethod
    def device_type(self) -> str:
        """A short string identifying the device category.

        :returns: Device type identifier (e.g. ``'vcpu'``, ``'memory'``,
            ``'harddrive'``, ``'network_ref'``).
        """


__all__ = ["AbstractDevice"]
