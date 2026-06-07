"""Libvirt-specific disk variants: pick the guest-visible controller ``bus``.

These subclass the portable disk types (:mod:`testrange.devices.disk`) to expose
the one knob the libvirt backend can vary that the portable types don't: the
controller a disk hangs off. A plan that uses one is, by construction, pinned to
the libvirt backend (the portability lint
:func:`testrange.orchestrator.backend.compatibility_findings` is the hook that
rejects binding such a plan elsewhere).

The motivating case is a **nested ESXi guest**: ESXi ships no virtio-blk driver,
so its OS disk must hang off ``sata`` or ``ide`` rather than the libvirt default
``virtio``. A plain :class:`~testrange.devices.OSDrive` /
:class:`~testrange.devices.HardDrive` keeps the virtio-blk default.
"""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.disk.base import HardDrive, OSDrive, _Disk

# Controllers libvirt/QEMU can attach a disk to. ``virtio`` (virtio-blk) presents
# to the guest as /dev/vd*; ``sata``/``scsi`` as /dev/sd*; ``ide`` as /dev/hd*.
LIBVIRT_DISK_BUSES = frozenset({"virtio", "sata", "ide", "scsi"})


@dataclass(frozen=True)
class _LibvirtDisk(_Disk):
    """Shared base for the libvirt disk variants — adds the controller ``bus``.

    Not used directly; :class:`LibvirtOSDrive` / :class:`LibvirtDataDrive` are the
    concrete types. ``bus`` defaults to ``virtio`` so a libvirt variant with no
    explicit bus behaves like the plain disk it specializes.
    """

    bus: str = "virtio"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bus not in LIBVIRT_DISK_BUSES:
            raise ValueError(
                f"{type(self).__name__}.bus must be one of {sorted(LIBVIRT_DISK_BUSES)}, "
                f"got {self.bus!r}"
            )


@dataclass(frozen=True)
class LibvirtOSDrive(_LibvirtDisk, OSDrive):
    """The OS disk on a chosen libvirt controller ``bus`` (default virtio-blk)."""


@dataclass(frozen=True)
class LibvirtDataDrive(_LibvirtDisk, HardDrive):
    """A data disk on a chosen libvirt controller ``bus`` (default virtio-blk)."""


__all__ = ["LIBVIRT_DISK_BUSES", "LibvirtDataDrive", "LibvirtOSDrive"]
