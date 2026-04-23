"""Virtual hard drive device."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice
from testrange.devices.sizes import normalise_qemu_size, parse_size

_HARD_DRIVE_BUSES = ("virtio", "sata", "nvme", "scsi", "ide")
"""Supported :attr:`HardDrive.bus` values.  Backends map these to
their own device models; most backends understand the strings
directly."""


class HardDrive(AbstractDevice):
    """Specifies a virtual hard drive to attach to a VM.

    Multiple ``HardDrive`` entries in a VM's ``devices=[...]`` list
    attach multiple disks.  **The first entry is always the OS disk**
    — it's the one cloud-init installs onto and the one whose
    post-install snapshot lands in the cache.  Every subsequent
    ``HardDrive`` is provisioned as an empty data volume
    (``<vm>-data<n>.qcow2`` in the per-run scratch dir) that the guest
    sees as ``/dev/vdb``, ``/dev/vdc``, etc.

    :param size: Disk size.  Accepts either a numeric value interpreted
        as GiB (``HardDrive(32)`` → 32 GiB) or a human-readable string
        (``HardDrive("64GB")``, ``HardDrive("512M")``, ``HardDrive("1T")``).
        Defaults to ``"20GB"``.
    :param bus: Disk bus.  One of ``"virtio"``, ``"sata"``, ``"nvme"``,
        ``"scsi"``, ``"ide"``.  ``None`` (the default) lets the backend
        pick — typically ``"virtio"`` for Linux guests and ``"sata"``
        for Windows (driver-free install).
    :param nvme: Shortcut for ``bus="nvme"``.  Kept for ergonomics —
        ``HardDrive(2000, nvme=True)`` reads nicely.  ``bus=`` wins
        when both are set.

    Example::

        devices=[
            HardDrive(32),                # 32 GiB primary (OS) disk
            HardDrive(100),               # 100 GiB data disk
            HardDrive("2TB", nvme=True),  # 2 TB NVMe data disk
            HardDrive(500, bus="scsi"),   # 500 GiB virtio-scsi data disk
        ]
    """

    size: str
    """Normalised disk size string (e.g. ``'64GB'``, ``'32GiB'``)."""

    bus: str | None
    """Explicit bus selector, or ``None`` to let the backend default."""

    def __init__(
        self,
        size: int | float | str = "20GB",
        nvme: bool = False,
        bus: str | None = None,
    ) -> None:
        # Numeric sizes are interpreted as GiB for ergonomics — most
        # disk sizes in practice are whole-GiB counts, and it reads
        # naturally: ``HardDrive(32)`` rather than ``HardDrive("32GiB")``.
        if isinstance(size, (int, float)):
            if size <= 0:
                raise ValueError(f"HardDrive size must be > 0 GiB, got {size}")
            size = f"{size}GiB"
        # Validate size at construction time to give early feedback.
        parse_size(size)
        self.size = size

        # Resolve bus: explicit wins; else fall back to the nvme
        # shortcut; else ``None`` (backend decides).
        if bus is not None:
            if bus not in _HARD_DRIVE_BUSES:
                raise ValueError(
                    f"HardDrive bus={bus!r} is not one of "
                    f"{_HARD_DRIVE_BUSES}"
                )
            self.bus = bus
        elif nvme:
            self.bus = "nvme"
        else:
            self.bus = None

    @property
    def nvme(self) -> bool:
        """``True`` iff :attr:`bus` is ``"nvme"``.

        Kept for backward compatibility with call sites that want a
        quick boolean check instead of string comparison.
        """
        return self.bus == "nvme"

    @property
    def size_bytes(self) -> int:
        """Return the requested size in bytes.

        :returns: Size in bytes.
        """
        return parse_size(self.size)

    @property
    def qemu_size(self) -> str:
        """Return the size string in the canonical ``<integer>G`` form
        backends feed to their disk-sizing tools (e.g. ``'64G'``).

        :returns: Normalised size string.
        """
        return normalise_qemu_size(self.size)

    def resolved_bus(self, *, windows: bool = False) -> str:
        """Return the bus the backend should render for this drive.

        :param windows: ``True`` when the guest is Windows — flips
            the implicit default from virtio to SATA so Setup can
            see the disk without virtio-blk drivers.
        :returns: One of :data:`_HARD_DRIVE_BUSES`.
        """
        if self.bus is not None:
            return self.bus
        return "sata" if windows else "virtio"

    @property
    def device_type(self) -> str:
        """Return ``'harddrive'``.

        :returns: The string ``'harddrive'``.
        """
        return "harddrive"

    def __repr__(self) -> str:
        if self.bus == "nvme":
            return f"HardDrive({self.size!r}, nvme=True)"
        if self.bus is not None:
            return f"HardDrive({self.size!r}, bus={self.bus!r})"
        return f"HardDrive({self.size!r})"


__all__ = ["HardDrive"]
