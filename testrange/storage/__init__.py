"""Storage backends — where the hypervisor's disks live.

A :class:`StorageBackend` is the minimal file + ``qemu-img`` surface the
orchestrator needs to put bytes where a hypervisor can read them, plus
the handful of image-manipulation primitives every backend builds on.

Why this exists
---------------

Every non-trivial TestRange feature comes back to the same question:
"where does this qcow2 live, and how do I put one there?"  For the
local KVM case that's a filesystem path + ``subprocess.run(qemu-img)``.
For ``qemu+ssh://remote/system`` it's SFTP + ``ssh remote qemu-img``.
For a future Proxmox backend it's a REST upload + a storage-volume
identifier.  Pre-:class:`StorageBackend` code assumed the local answer
everywhere and silently broke on every other backend.

Implementations
---------------

- :class:`LocalStorageBackend` — outer host's filesystem + local
  ``qemu-img``.  The default; preserves today's behaviour bit-for-bit.
- :class:`SSHStorageBackend` — remote host via paramiko SFTP + SSH
  exec.  Turns ``Orchestrator(host="qemu+ssh://box/system")`` into
  a working setup for the first time.
"""

from testrange.storage.base import AbstractStorageBackend
from testrange.storage.local import LocalStorageBackend
from testrange.storage.ssh import SSHStorageBackend

__all__ = [
    "AbstractStorageBackend",
    "LocalStorageBackend",
    "SSHStorageBackend",
]
