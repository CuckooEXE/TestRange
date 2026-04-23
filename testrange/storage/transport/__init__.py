"""File + exec transports.

A :class:`AbstractFileTransport` is the "which filesystem are we
touching, and how do we run commands against it" half of a storage
backend.  Pairs with an :class:`~testrange.storage.disk.AbstractDiskFormat`
to form a full :class:`~testrange.storage.StorageBackend`.

Implementations:

- :class:`LocalFileTransport` — outer host's filesystem + local
  subprocess.  Identity wrapper; preserves today's behaviour for
  ``Orchestrator(host="localhost")``.
- :class:`SSHFileTransport` — paramiko SFTP + SSH exec to a remote
  host.  Lets ``Orchestrator(host="qemu+ssh://...")`` put bytes on
  the remote and execute tools there.

Third-party transports (Proxmox REST, Hyper-V PSSession, nested-VM
communicator, …) land alongside these by subclassing
:class:`AbstractFileTransport`.
"""

from testrange.storage.transport.base import AbstractFileTransport
from testrange.storage.transport.local import LocalFileTransport
from testrange.storage.transport.ssh import SSHFileTransport

__all__ = [
    "AbstractFileTransport",
    "LocalFileTransport",
    "SSHFileTransport",
]
