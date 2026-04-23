"""Storage backends — where the hypervisor's disks live.

A :class:`StorageBackend` composes two orthogonal abstractions:

- :class:`~testrange.storage.transport.AbstractFileTransport`
  — "which filesystem is the hypervisor reading from, and how do I
  run commands against it?"
- :class:`~testrange.storage.disk.AbstractDiskFormat`
  — "what image format lives there, and what operations are defined
  for it?"

Every non-trivial TestRange feature comes back to the same question:
"where does this image live, and how do I manipulate one?"  Local
KVM is a filesystem path + ``qemu-img`` subprocess.  Remote KVM is
SFTP + ``ssh remote qemu-img``.  A future Hyper-V host is SMB /
PSSession + PowerShell ``New-VHD``.  A future Proxmox backend is a
REST upload + a storage-volume identifier.  Decomposing into
(transport, format) means adding a new transport doesn't force every
format to re-learn it, and adding a new format doesn't force every
transport to re-learn it.

Shipped concrete pairings:

- :class:`LocalStorageBackend` — local filesystem + qcow2.
- :class:`SSHStorageBackend` — SFTP/SSH + qcow2.

Callers that need a custom pairing build a :class:`StorageBackend`
directly with the transport and format they want.
"""

from testrange.storage.base import (
    AbstractStorageBackend,
    LocalStorageBackend,
    SSHStorageBackend,
    StorageBackend,
)
from testrange.storage.disk import AbstractDiskFormat, Qcow2DiskFormat
from testrange.storage.transport import (
    AbstractFileTransport,
    LocalFileTransport,
    SSHFileTransport,
)

__all__ = [
    # Composition + convenience pairings
    "StorageBackend",
    "LocalStorageBackend",
    "SSHStorageBackend",
    "AbstractStorageBackend",  # legacy alias for StorageBackend
    # Transport axis
    "AbstractFileTransport",
    "LocalFileTransport",
    "SSHFileTransport",
    # Disk-format axis
    "AbstractDiskFormat",
    "Qcow2DiskFormat",
]
