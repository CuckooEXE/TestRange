"""libvirt-flavoured pre-composed storage backends.

The generic :class:`~testrange.storage.StorageBackend` is just a
``(transport, disk_format)`` pair.  These two convenience subclasses
pin the disk format to qcow2 because that's what every libvirt
hypervisor reads natively — keeping the qcow2 binding here (rather
than in the generic storage layer) means a future Hyper-V backend
that wants ``SSHFileTransport + VhdxDiskFormat`` doesn't have to
work around an in-the-way ``SSHStorageBackend`` shaped like
``SSH+qcow2``.

Other backends:

* compose :class:`~testrange.storage.StorageBackend` directly with
  the matching transport + disk format, or
* publish their own pre-composed convenience subclasses in their
  backend module (the way these do here).
"""

from __future__ import annotations

from pathlib import Path

from testrange.storage.base import StorageBackend
from testrange.storage.disk.qcow2 import Qcow2DiskFormat
from testrange.storage.transport.local import LocalFileTransport
from testrange.storage.transport.ssh import SSHFileTransport


class LocalStorageBackend(StorageBackend):
    """Convenience: :class:`LocalFileTransport` + :class:`Qcow2DiskFormat`.

    What :class:`testrange.backends.libvirt.Orchestrator` auto-selects
    for ``host="localhost"`` (or any local libvirt URI).  The qcow2
    binding makes this libvirt-flavoured; backends with a different
    disk format compose :class:`StorageBackend` directly.
    """

    def __init__(self, cache_root: Path) -> None:
        transport = LocalFileTransport(cache_root)
        super().__init__(
            transport=transport,
            disk=Qcow2DiskFormat(transport),
        )


class SSHStorageBackend(StorageBackend):
    """Convenience: :class:`SSHFileTransport` + :class:`Qcow2DiskFormat`.

    What :class:`testrange.backends.libvirt.Orchestrator` auto-selects
    for ``host="qemu+ssh://..."``.  All keyword args forward to
    :class:`SSHFileTransport`.  The qcow2 binding makes this
    libvirt-flavoured; a future Hyper-V backend that wants SSH-reached
    storage with VHDX would compose its own pairing instead.
    """

    def __init__(
        self,
        host: str,
        username: str | None = None,
        port: int = 22,
        key_filename: str | None = None,
        cache_root: str | None = None,
        connect_timeout: float = 30.0,
    ) -> None:
        transport = SSHFileTransport(
            host=host,
            username=username,
            port=port,
            key_filename=key_filename,
            cache_root=cache_root,
            connect_timeout=connect_timeout,
        )
        super().__init__(
            transport=transport,
            disk=Qcow2DiskFormat(transport),
        )


__all__ = ["LocalStorageBackend", "SSHStorageBackend"]
