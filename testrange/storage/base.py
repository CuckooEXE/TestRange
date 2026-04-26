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

from testrange.storage.disk.base import AbstractDiskFormat
from testrange.storage.transport.base import AbstractFileTransport


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


# Pre-composed convenience subclasses (e.g. ``LocalFileTransport +
# Qcow2DiskFormat``) that pin a specific disk format are
# **backend-flavoured** — the format binding is the libvirt-leaning
# bit.  Each backend that wants its own pairings publishes them in
# its backend module (see :mod:`testrange.backends.libvirt.storage`).
# The generic storage layer here intentionally stays format-agnostic.


# ---------------------------------------------------------------------------
# Legacy alias for callers that imported the old ABC.
# ---------------------------------------------------------------------------

AbstractStorageBackend = StorageBackend
"""Legacy alias preserved so ``from testrange.storage import
AbstractStorageBackend`` keeps working.  The class is no longer
``abstract`` in the strict sense — it's a concrete composition —
but the name is retained for import compatibility."""
