"""Proxmox-specific device value objects.

These subclass the portable device types (``testrange.devices``) to expose knobs
that only exist on Proxmox. A plan that uses one is, by construction, pinned to
the Proxmox backend — the portability lint
(:func:`testrange.orchestrator.backend.compatibility_findings`) is the hook that
would reject binding such a plan to another backend. They belong in
``examples/capabilities-px.py`` (driver-specific showcase), never the portable
``examples/capabilities.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.disk.base import HardDrive

# Controllers PVE can attach a disk to. scsi/sata/ide present to the guest as
# /dev/sd*; virtio (virtio-blk) presents as /dev/vd*.
PVE_DISK_BUSES = frozenset({"scsi", "virtio", "sata", "ide"})


@dataclass(frozen=True)
class ProxmoxHardDrive(HardDrive):
    """A data disk attached on a chosen PVE controller ``bus`` (default ``scsi``).

    ``bus`` selects the guest-visible controller, so a plan can prove a specific
    device model end-to-end: ``scsi``/``sata``/``ide`` → ``/dev/sd*``,
    ``virtio`` → ``/dev/vd*``. A plain :class:`~testrange.devices.HardDrive`
    attaches on the driver's default (``scsi`` on Proxmox); this makes the choice
    explicit and per-disk.
    """

    bus: str = "scsi"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bus not in PVE_DISK_BUSES:
            raise ValueError(
                f"ProxmoxHardDrive.bus must be one of {sorted(PVE_DISK_BUSES)}, got {self.bus!r}"
            )
