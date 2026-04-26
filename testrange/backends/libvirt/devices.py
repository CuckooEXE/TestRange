"""libvirt-specific virtual hardware devices.

Sibling subclasses of the abstract device bases in
:mod:`testrange.devices`.  Use these (instead of the generic
counterparts) when you need libvirt-specific knobs — bus selection,
the NVMe shortcut, model overrides, etc.

Type-system contract: a libvirt-specific device passed to a
different backend's VM is a type error caught by pyright before the
test runs.  See :mod:`testrange.backends.libvirt.vm` for the
``devices=`` union type each VM class declares.
"""

from __future__ import annotations

from typing import Literal

from testrange.devices.hard_drive import AbstractHardDrive
from testrange.devices.sizes import parse_size

LibvirtDriveBus = Literal["virtio", "sata", "nvme", "scsi", "ide"]
"""Disk bus values libvirt understands; backends map directly."""

_LIBVIRT_DRIVE_BUSES: tuple[LibvirtDriveBus, ...] = (
    "virtio", "sata", "nvme", "scsi", "ide",
)


class LibvirtHardDrive(AbstractHardDrive):
    """libvirt-specific hard drive with bus selection and NVMe shortcut.

    Use this (instead of :class:`testrange.HardDrive`) when you want
    to pin the drive's bus or take advantage of the NVMe shortcut.
    Carries everything :class:`testrange.HardDrive` does plus libvirt-
    specific options.

    :param size: Disk size — same accepted forms as
        :class:`testrange.HardDrive`.
    :param bus: Disk bus.  One of ``"virtio"``, ``"sata"``, ``"nvme"``,
        ``"scsi"``, ``"ide"``.  ``None`` (default) lets the backend
        pick — typically ``"virtio"`` for Linux guests and ``"sata"``
        for Windows (driver-free install).
    :param nvme: Shortcut for ``bus="nvme"``.  Reads more naturally
        for the common case (``LibvirtHardDrive(2000, nvme=True)``).
        ``bus=`` wins when both are set.

    Example::

        from testrange.backends.libvirt import LibvirtHardDrive

        devices=[
            LibvirtHardDrive("2TB", nvme=True),  # 2 TB NVMe data disk
            LibvirtHardDrive(500, bus="scsi"),   # 500 GiB virtio-scsi
        ]
    """

    bus: LibvirtDriveBus | None
    """Explicit bus selector, or ``None`` to let the backend default."""

    def __init__(
        self,
        size: int | float | str = "20GB",
        nvme: bool = False,
        bus: LibvirtDriveBus | None = None,
    ) -> None:
        if isinstance(size, (int, float)):
            if size <= 0:
                raise ValueError(
                    f"LibvirtHardDrive size must be > 0 GiB, got {size}"
                )
            size = f"{size}GiB"
        parse_size(size)
        self.size = size

        if bus is not None:
            if bus not in _LIBVIRT_DRIVE_BUSES:
                raise ValueError(
                    f"LibvirtHardDrive bus={bus!r} is not one of "
                    f"{_LIBVIRT_DRIVE_BUSES}"
                )
            self.bus = bus
        elif nvme:
            self.bus = "nvme"
        else:
            self.bus = None

    @property
    def nvme(self) -> bool:
        """``True`` iff :attr:`bus` is ``"nvme"``."""
        return self.bus == "nvme"

    def display_tag(self) -> str:
        """Surface NVMe in human-facing topology renders.

        Other buses pick the backend's default rendering so the
        topology view stays terse for the common case.
        """
        return " NVMe" if self.nvme else ""

    def resolved_bus(self, *, windows: bool = False) -> str:
        """Return the bus the backend should render for this drive.

        :param windows: ``True`` when the guest is Windows — flips
            the implicit default from virtio to SATA so Setup can
            see the disk without virtio-blk drivers.
        """
        if self.bus is not None:
            return self.bus
        return "sata" if windows else "virtio"

    def __repr__(self) -> str:
        if self.bus == "nvme":
            return f"LibvirtHardDrive({self.size!r}, nvme=True)"
        if self.bus is not None:
            return f"LibvirtHardDrive({self.size!r}, bus={self.bus!r})"
        return f"LibvirtHardDrive({self.size!r})"


__all__ = ["LibvirtDriveBus", "LibvirtHardDrive"]
