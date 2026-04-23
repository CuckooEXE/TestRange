"""Abstract disk-format operations.

Parameterised over an :class:`~testrange.storage.transport.AbstractFileTransport`.
Subclasses implement a specific image format (qcow2, VHDX, VMDK, a
Proxmox storage-pool volume type, …) by running the appropriate
tool through the transport — the format knows *what* command to run;
the transport knows *where* to run it.

This split exists so adding a new transport (remote host, nested VM,
REST API) doesn't duplicate the tool-argv logic for every format, and
adding a new format doesn't require knowing whether the filesystem is
local or remote.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange.storage.transport.base import AbstractFileTransport


class AbstractDiskFormat(ABC):
    """Disk-image operations routed through a transport.

    Each concrete subclass implements a single disk format.  The
    transport is supplied at construction and stored as
    ``_transport``; all disk ops funnel through its
    :meth:`~AbstractFileTransport.run_tool` primitive.

    :param transport: The file transport whose
        :meth:`~AbstractFileTransport.run_tool` will execute the
        image-manipulation commands for this format.  Typically the
        same transport the enclosing :class:`StorageBackend` owns.
    """

    _transport: AbstractFileTransport

    def __init__(self, transport: AbstractFileTransport) -> None:
        self._transport = transport

    @abstractmethod
    def create_overlay(self, backing_ref: str, dest_ref: str) -> None:
        """Create a copy-on-write overlay on top of *backing_ref*.

        The overlay at *dest_ref* refers to *backing_ref* as its
        backing store; writes land in the overlay, reads fall through.

        :raises CacheError: On tool failure.
        """

    @abstractmethod
    def create_blank(self, dest_ref: str, size: str) -> None:
        """Create an empty disk at *dest_ref* of the given *size*.

        *size* accepts a format-neutral ``<integer>G`` / ``<integer>M``
        string; implementations parse it for their tool's expected
        syntax.

        :raises CacheError: On tool failure.
        """

    @abstractmethod
    def resize(self, ref: str, size: str) -> None:
        """Resize the disk at *ref* to *size*.

        :param size: Absolute (``'64G'``) or delta (``'+20G'``).
        :raises CacheError: On tool failure.
        """

    @abstractmethod
    def compress(self, src_ref: str, dest_ref: str) -> None:
        """Produce a compressed copy of *src_ref* at *dest_ref*.

        Used when promoting a freshly-installed disk into the
        persistent snapshot cache — the archived copy is compressed
        so the cache stays small.

        :raises CacheError: On tool failure.
        """
