"""Virtual hard drive devices.

Two-tier hierarchy:

* :class:`AbstractHardDrive` — sealed abstract base.  Backend-specific
  drives subclass this directly so they're **siblings** of the
  generic :class:`HardDrive`, not children — that's the key to making
  pyright catch one backend's drive being passed to another backend's
  VM (an inheritance hierarchy would let the type check pass).
* :class:`HardDrive` — the generic disk every backend accepts.  Just
  a size; the backend picks a sensible bus and other format-specific
  flags.  Use this when you want a portable spec.

Backend-specific drives live in each backend's module under
``testrange.backends.<backend>`` and expose whatever extra knobs that
backend's disk-attachment layer supports (bus selection, storage pool,
cache mode, …).
"""

from __future__ import annotations

from testrange.devices.base import AbstractDevice
from testrange.devices.sizes import normalise_size, parse_size


class AbstractHardDrive(AbstractDevice):
    """Sealed base class for every variety of virtual hard drive.

    Subclasses set :attr:`size` (the only universal field) and may
    add backend-specific fields of their own.  Subclasses of this
    are **siblings**, not children of one another — that's how the
    type system catches a backend's drive being passed to a
    different backend's VM.  Do not subclass :class:`AbstractDevice`
    directly for drive-shaped devices.
    """

    size: str
    """Normalised disk size string (e.g. ``'64GB'``, ``'32GiB'``)."""

    @property
    def device_type(self) -> str:
        return "harddrive"

    @property
    def size_bytes(self) -> int:
        """Return the requested size in bytes."""
        return parse_size(self.size)

    @property
    def size_string(self) -> str:
        """Return the size string in the canonical ``<integer>G`` form
        backends feed to their disk-sizing tools (e.g. ``'64G'``)."""
        return normalise_size(self.size)

    def display_tag(self) -> str:
        """Short label appended to this drive in human-facing renders
        (the ``testrange show`` topology output, log lines).

        Backend-specific drives override to surface their backend-
        specific knobs (e.g. ``" NVMe"``, ``" cache=writeback"``).
        Default returns the empty string — the generic
        :class:`HardDrive` has nothing extra to advertise.
        """
        return ""


class HardDrive(AbstractHardDrive):
    """Generic virtual hard drive — accepted by every backend.

    Carries only the fields every hypervisor needs (size).  Backends
    pick sensible defaults for bus, cache mode, and any format-
    specific flags.  When you need backend-specific knobs, import
    the matching ``<Backend>HardDrive`` subclass from that backend's
    module instead.

    Multiple ``HardDrive`` entries in a VM's ``devices=[...]`` list
    attach multiple disks.  **The first entry is always the OS disk**
    — the one cloud-init installs onto and the one whose post-install
    snapshot lands in the cache.  Every subsequent ``HardDrive`` is
    provisioned as an empty data volume in the per-run scratch dir
    (file extension owned by the backend's disk format).

    :param size: Disk size.  Accepts either a numeric value
        interpreted as GiB (``HardDrive(32)`` → 32 GiB) or a
        human-readable string (``HardDrive("64GB")``,
        ``HardDrive("512M")``, ``HardDrive("1T")``).  Defaults to
        ``"20GB"``.

    Example::

        devices=[
            HardDrive(32),    # 32 GiB primary (OS) disk
            HardDrive(100),   # 100 GiB data disk
        ]
    """

    def __init__(self, size: int | float | str = "20GB") -> None:
        # Numeric sizes are interpreted as GiB for ergonomics — most
        # disk sizes in practice are whole-GiB counts, and it reads
        # naturally: ``HardDrive(32)`` rather than ``HardDrive("32GiB")``.
        if isinstance(size, (int, float)):
            if size <= 0:
                raise ValueError(f"HardDrive size must be > 0 GiB, got {size}")
            size = f"{size}GiB"
        # Validate at construction time so bad sizes fail before any
        # backend sees them.
        parse_size(size)
        self.size = size

    def __repr__(self) -> str:
        return f"HardDrive({self.size!r})"


__all__ = ["AbstractHardDrive", "HardDrive"]
