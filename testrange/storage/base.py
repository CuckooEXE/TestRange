"""Storage backend — the composition of a file transport and a disk
format.

A :class:`StorageBackend` bundles two orthogonal abstractions:

- ``transport`` (:class:`~testrange.storage.transport.AbstractFileTransport`)
  — "which filesystem does the hypervisor read from, and how do I run
  commands against it?"
- ``disk`` (:class:`~testrange.storage.disk.AbstractDiskFormat`)
  — "what image format are those files, and what commands operate on
  them?"

Call sites use either explicitly::

    # File / exec primitives — transport concerns
    run.storage.transport.write_bytes(ref, data)
    run.storage.transport.upload(local_path, ref)

    # Disk / image primitives — format concerns
    run.storage.disk.create_overlay(backing_ref, dest_ref)
    run.storage.disk.resize(ref, "64G")

The split matters because the two axes are genuinely independent.
Local KVM, remote KVM-over-SSH, and nested KVM-via-communicator all
share the same disk format (qcow2) but differ entirely in transport;
a future Hyper-V backend would share the transport story (SSH /
PSSession / whatever) with one of those but swap the format for
VHDX.  Keeping them separate means adding a new transport doesn't
duplicate the per-format tool-argv logic, and adding a new format
doesn't require knowing whether its target filesystem is local.
"""

from __future__ import annotations

from pathlib import Path

from testrange.storage.disk.base import AbstractDiskFormat
from testrange.storage.disk.qcow2 import Qcow2DiskFormat
from testrange.storage.transport.base import AbstractFileTransport
from testrange.storage.transport.local import LocalFileTransport
from testrange.storage.transport.ssh import SSHFileTransport


class StorageBackend:
    """A (transport, disk-format) pair.

    :param transport: File + exec primitives.
    :param disk: Disk-image manipulation primitives, parameterised
        over *transport* (so its tool invocations land on the right
        host).
    """

    transport: AbstractFileTransport
    """File + exec primitives.  Caller uses as ``backend.transport.xxx``."""

    disk: AbstractDiskFormat
    """Disk-format primitives.  Caller uses as ``backend.disk.xxx``."""

    def __init__(
        self,
        transport: AbstractFileTransport,
        disk: AbstractDiskFormat,
    ) -> None:
        self.transport = transport
        self.disk = disk

    def close(self) -> None:
        """Release any transport-level resources.

        Idempotent, never raises.  Delegates to the transport's
        ``close()`` if it has one (SSH transports have a connection
        to tear down; local doesn't).
        """
        close = getattr(self.transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Pre-composed convenience subclasses.
#
# The vast majority of current users want "local filesystem + qcow2"
# or "SSH-reachable remote filesystem + qcow2".  Expose those as
# single-argument constructors so ``Orchestrator(host=…)``'s
# auto-selection stays a one-liner per URI shape; callers with more
# exotic pairings (different transport × different format) build a
# :class:`StorageBackend` directly.
# ---------------------------------------------------------------------------


class LocalStorageBackend(StorageBackend):
    """Convenience: :class:`LocalFileTransport` + :class:`Qcow2DiskFormat`.

    Pre-composed pairing that fits any backend whose hypervisor reads
    qcow2 from a local filesystem (libvirt's local URIs being the
    canonical case).  Backends with a different disk format compose
    a :class:`StorageBackend` directly with their own
    :class:`AbstractDiskFormat`; backends with a different transport
    pick or build the matching transport class.
    """

    def __init__(self, cache_root: Path) -> None:
        transport = LocalFileTransport(cache_root)
        super().__init__(
            transport=transport,
            disk=Qcow2DiskFormat(transport),
        )


class SSHStorageBackend(StorageBackend):
    """Convenience: :class:`SSHFileTransport` + :class:`Qcow2DiskFormat`.

    Pre-composed pairing for any backend whose hypervisor reads qcow2
    over SSH-reachable storage (libvirt's ``qemu+ssh://`` form being
    the canonical case).  Backends using a different transport
    (WinRM, SMB, REST) or a different disk format compose a
    :class:`StorageBackend` directly with the matching components.
    All keyword args forward to :class:`SSHFileTransport`.
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


# ---------------------------------------------------------------------------
# Legacy alias for callers that imported the old ABC.
# ---------------------------------------------------------------------------

AbstractStorageBackend = StorageBackend
"""Legacy alias preserved so ``from testrange.storage import
AbstractStorageBackend`` keeps working.  The class is no longer
``abstract`` in the strict sense — it's a concrete composition —
but the name is retained for import compatibility."""
