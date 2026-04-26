"""Storage backends — where the hypervisor's disks live.

A :class:`StorageBackend` composes two orthogonal abstractions:

- :class:`~testrange.storage.transport.AbstractFileTransport`
  — "which filesystem is the hypervisor reading from, and how do I
  run commands against it?"
- :class:`~testrange.storage.disk.AbstractDiskFormat`
  — "what image format lives there, and what operations are defined
  for it?"

Every non-trivial TestRange feature comes back to the same question:
"where does this image live, and how do I manipulate one?"  A
local hypervisor is a filesystem path + a local CLI subprocess.  A
remote hypervisor is SFTP + the same CLI run via SSH.  A future
SMB/WinRM host is its own transport + its own image-creation tool.
A future REST-driven host is a REST upload + a storage-volume
identifier.  Decomposing into (transport, format) means adding a
new transport doesn't force every format to re-learn it, and
adding a new format doesn't force every transport to re-learn it.

Shipped pieces here are deliberately format-agnostic:

- The generic :class:`StorageBackend` composer.
- Both transports (:class:`LocalFileTransport`,
  :class:`SSHFileTransport`).
- The disk-format ABC plus a default disk-format implementation.

Pre-composed pairings (transport + disk-format) are
**backend-flavoured** — the disk-format binding is what pins a
pairing to a specific hypervisor family.  Each backend publishes
its own convenience subclasses in its backend module under
``testrange.backends.<backend>``.

Callers that want an exotic pairing compose a
:class:`StorageBackend` directly with the transport + format they
need.
"""

from testrange.storage.base import (
    AbstractStorageBackend,
    StorageBackend,
)
from testrange.storage.disk import AbstractDiskFormat
from testrange.storage.transport import (
    AbstractFileTransport,
    LocalFileTransport,
    SSHFileTransport,
)

__all__ = [
    # Composition
    "StorageBackend",
    "AbstractStorageBackend",  # legacy alias for StorageBackend
    # Transport axis
    "AbstractFileTransport",
    "LocalFileTransport",
    "SSHFileTransport",
    # Disk-format axis (concrete formats live in their backend module)
    "AbstractDiskFormat",
]
