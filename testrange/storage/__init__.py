"""Storage backends — where the hypervisor's disks live.

A :class:`StorageBackend` is the minimal file + image-manipulation
surface the orchestrator needs to put bytes where a hypervisor can
read them.

Why this exists
---------------

Every non-trivial TestRange feature comes back to the same question:
"where does this qcow2 live, and how do I put one there?"  For a
local-host hypervisor that's a filesystem path + a local subprocess.
For an SSH-reachable remote hypervisor it's SFTP + remote exec.  For
an API-driven one (REST / RPC) it's an upload endpoint + a
storage-volume identifier.  Pre-:class:`StorageBackend` code assumed
the local answer everywhere and silently broke for every other shape.

Implementations
---------------

- :class:`LocalStorageBackend` — outer host's filesystem + local
  subprocess.  The default when the orchestrator's control plane
  lives on the same machine as the Python process.
- :class:`SSHStorageBackend` — remote host via paramiko SFTP + SSH
  exec.  Used when the orchestrator talks to a hypervisor over SSH.
"""

from testrange.storage.base import AbstractStorageBackend
from testrange.storage.local import LocalStorageBackend
from testrange.storage.ssh import SSHStorageBackend

__all__ = [
    "AbstractStorageBackend",
    "LocalStorageBackend",
    "SSHStorageBackend",
]
