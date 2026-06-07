"""ESXi-specific device value objects.

These subclass the portable device types (``testrange.devices``) to expose knobs
that only exist on ESXi. A plan that uses one is, by construction, pinned to the
ESXi backend — the portability lint
(:func:`testrange.orchestrator.backend.compatibility_findings`) rejects binding
such a plan to another backend. They belong in ``examples/capabilities-esxi.py``
(driver-specific showcase, ESXI-15), never the portable ``examples/capabilities.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.disk.base import HardDrive

# Controller buses ESXi can attach a disk to. scsi/sata/ide present to the guest
# as /dev/sd*; nvme presents as /dev/nvme*. (No virtio-blk on ESXi — a plan that
# expects /dev/vd* is libvirt/Proxmox-pinned.)
ESXI_DISK_BUSES = frozenset({"scsi", "sata", "ide", "nvme"})

# Guest NIC models ESXi offers. VMXNET3 is the paravirtual default (best perf,
# needs VMware Tools' driver — present in modern Linux); e1000/e1000e are
# emulated and work without tools (useful for a bare installer).
ESXI_NIC_MODELS = frozenset({"vmxnet3", "e1000", "e1000e"})
DEFAULT_NIC_MODEL = "vmxnet3"


@dataclass(frozen=True)
class ESXiHardDrive(HardDrive):
    """A data disk attached on a chosen ESXi controller ``bus`` (default ``scsi``).

    ``bus`` selects the guest-visible controller: ``scsi``/``sata``/``ide`` ->
    ``/dev/sd*``, ``nvme`` -> ``/dev/nvme*``. A plain
    :class:`~testrange.devices.HardDrive` attaches on the driver default
    (``scsi``); this makes the choice explicit and per-disk.
    """

    bus: str = "scsi"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bus not in ESXI_DISK_BUSES:
            raise ValueError(
                f"ESXiHardDrive.bus must be one of {sorted(ESXI_DISK_BUSES)}, got {self.bus!r}"
            )
