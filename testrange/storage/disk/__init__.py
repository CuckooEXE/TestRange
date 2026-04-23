"""Disk-format operations.

An :class:`AbstractDiskFormat` is the "what image format is the
hypervisor reading" half of a storage backend.  Pairs with an
:class:`~testrange.storage.transport.AbstractFileTransport` to form a
full :class:`~testrange.storage.StorageBackend`.

Implementations:

- :class:`Qcow2DiskFormat` — qcow2 via ``qemu-img``, for the
  QEMU/KVM family (libvirt, Proxmox-with-qcow2, …).

Future implementations plug in here: ``VhdxDiskFormat`` would run
PowerShell ``New-VHD`` / ``Resize-VHD`` / ``Convert-VHD`` against a
transport whose ``run_tool`` reaches a Windows host.  No changes
needed in the ABC for a new format — implementations just subclass
:class:`AbstractDiskFormat` and call through their ``_transport``.
"""

from testrange.storage.disk.base import AbstractDiskFormat
from testrange.storage.disk.qcow2 import Qcow2DiskFormat

__all__ = [
    "AbstractDiskFormat",
    "Qcow2DiskFormat",
]
