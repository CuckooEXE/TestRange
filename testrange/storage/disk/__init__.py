"""Disk-format operations.

An :class:`AbstractDiskFormat` is the "what image format is the
hypervisor reading" half of a storage backend.  Pairs with an
:class:`~testrange.storage.transport.AbstractFileTransport` to form a
full :class:`~testrange.storage.StorageBackend`.

Only the abstract base lives here — concrete format implementations
live in their owning backend module under :mod:`testrange.backends`,
since the disk format is the part of a storage backend that pins the
pairing to a specific hypervisor family.
"""

from testrange.storage.disk.base import AbstractDiskFormat

__all__ = [
    "AbstractDiskFormat",
]
