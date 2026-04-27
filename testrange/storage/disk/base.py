"""Abstract disk-format operations.

Parameterised over an :class:`~testrange.storage.transport.AbstractFileTransport`.
Subclasses implement a specific image format by running the
appropriate tool through the transport — the format knows *what*
command to run; the transport knows *where* to run it.

This split exists so adding a new transport (remote host, nested VM,
REST API) doesn't duplicate the tool-argv logic for every format, and
adding a new format doesn't require knowing whether the filesystem is
local or remote.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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

    primary_disk_filename: str = "disk"
    """Conventional filename of a VM's primary disk in this format.

    Generic code (cache layer) constructs disk paths by joining a
    containing directory with this filename, so each format owns its
    own extension/naming convention without the rest of the codebase
    needing to know what's inside.  Subclasses override with their
    canonical ``disk.<ext>`` filename for the format they implement.
    """

    @property
    def disk_extension(self) -> str:
        """Filename extension for disks in this format, with leading dot.

        Derived from :attr:`primary_disk_filename` by default (a
        ``disk.<ext>`` value yields ``".<ext>"``).  Used by per-run
        scratch helpers that name overlays after the VM rather than
        after the format.
        """
        if "." in self.primary_disk_filename:
            return "." + self.primary_disk_filename.split(".", 1)[1]
        return ""

    def __init__(self, transport: AbstractFileTransport) -> None:
        self._transport = transport

    def validate_source_image(self, path: Path) -> None:
        """Validate that *path* (on the outer host) is in this disk format.

        Used by :class:`~testrange.vms.builders.NoOpBuilder` to fail
        loudly when the user hands a prebuilt image whose format the
        backend can't actually use.  Default implementation accepts
        anything; format-specific subclasses override to inspect the
        image with the format's native introspection tool.

        :raises testrange.exceptions.VMBuildError: When *path* is not
            in the expected format.
        """

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
