"""Virtual hardware device definitions for VM configuration.

Provides an abstract base class and concrete device types: :class:`vCPU`,
:class:`Memory`, :class:`HardDrive`, and :class:`VirtualNetworkRef`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod


class AbstractDevice(ABC):
    """Base class for all virtual hardware devices attached to a VM.

    Subclass this to implement custom device types.  Devices are passed as a
    list to the ``devices=`` parameter of :class:`~testrange.vms.base.AbstractVM`.

    Example::

        class VirtualTPM(AbstractDevice):
            @property
            def device_type(self) -> str:
                return "vtpm"
    """

    @property
    @abstractmethod
    def device_type(self) -> str:
        """A short string identifying the device category.

        :returns: Device type identifier (e.g. ``'vcpu'``, ``'memory'``,
            ``'harddrive'``, ``'network_ref'``).
        """


class vCPU(AbstractDevice):
    """Specifies the number of virtual CPU cores for a VM.

    :param count: Number of vCPU cores to allocate.  Defaults to ``2``.

    Example::

        vCPU(4)   # allocate 4 virtual cores
    """

    count: int
    """Number of virtual CPU cores to allocate to the VM."""

    def __init__(self, count: int = 2) -> None:
        if count < 1:
            raise ValueError(f"vCPU count must be >= 1, got {count}")
        self.count = count

    @property
    def device_type(self) -> str:
        """Return ``'vcpu'``.

        :returns: The string ``'vcpu'``.
        """
        return "vcpu"

    def __repr__(self) -> str:
        return f"vCPU({self.count!r})"


class Memory(AbstractDevice):
    """Specifies the RAM allocation for a VM.

    :param gib: Memory size in gibibytes (GiB).  Defaults to ``2``.

    Example::

        Memory(8)   # allocate 8 GiB RAM
    """

    gib: float
    """RAM allocation in gibibytes (GiB)."""

    def __init__(self, gib: float = 2.0) -> None:
        if gib <= 0:
            raise ValueError(f"Memory must be > 0 GiB, got {gib}")
        self.gib = gib

    @property
    def kib(self) -> int:
        """Return memory size in kibibytes.

        :returns: Memory in KiB (rounded to nearest integer).
        """
        return round(self.gib * 1024 * 1024)

    @property
    def device_type(self) -> str:
        """Return ``'memory'``.

        :returns: The string ``'memory'``.
        """
        return "memory"

    def __repr__(self) -> str:
        return f"Memory({self.gib!r})"


_SIZE_UNITS: dict[str, int] = {
    "B":   1,
    "K":   1024,
    "KB":  1024,
    "M":   1024 ** 2,
    "MB":  1024 ** 2,
    "MIB": 1024 ** 2,
    "G":   1024 ** 3,
    "GB":  1024 ** 3,
    "GIB": 1024 ** 3,
    "T":   1024 ** 4,
    "TB":  1024 ** 4,
    "TIB": 1024 ** 4,
}
"""Mapping of size unit suffix (uppercase) to its byte multiplier."""

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
"""Compiled regex for parsing human-readable size strings (e.g. ``'64GB'``)."""


def parse_size(size: str) -> int:
    """Parse a human-readable size string into a number of bytes.

    Supports common suffixes: ``B``, ``K``/``KB``, ``M``/``MB``/``MiB``,
    ``G``/``GB``/``GiB``, ``T``/``TB``/``TiB`` (case-insensitive).

    :param size: Size string, e.g. ``'64GB'``, ``'512M'``, ``'1.5TiB'``.
    :returns: Size in bytes as an integer.
    :raises ValueError: If the string cannot be parsed.
    """
    m = _SIZE_RE.match(size)
    if not m:
        raise ValueError(f"Cannot parse size string: {size!r}")
    value, unit = float(m.group(1)), m.group(2).upper()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"Unknown size unit {unit!r} in {size!r}")
    return int(value * _SIZE_UNITS[unit])


def normalise_qemu_size(size: str) -> str:
    """Return the size string in the canonical ``<integer>G`` form used
    by the shipped backends' disk-sizing tools.

    Converts to the nearest GiB integer with a ``G`` suffix.

    :param size: Human-readable size string.
    :returns: String like ``'64G'``.
    """
    gib = parse_size(size) // (1024 ** 3)
    return f"{max(gib, 1)}G"


_HARD_DRIVE_BUSES = ("virtio", "sata", "nvme", "scsi", "ide")
"""Supported :attr:`HardDrive.bus` values.  Backends map these to
their own device models; most backends understand the strings
directly."""


class HardDrive(AbstractDevice):
    """Specifies a virtual hard drive to attach to a VM.

    Multiple ``HardDrive`` entries in a VM's ``devices=[...]`` list
    attach multiple disks.  **The first entry is always the OS disk**
    — it's the one cloud-init installs onto and the one whose
    post-install snapshot lands in the cache.  Every subsequent
    ``HardDrive`` is provisioned as an empty data volume
    (``<vm>-data<n>.qcow2`` in the per-run scratch dir) that the guest
    sees as ``/dev/vdb``, ``/dev/vdc``, etc.

    :param size: Disk size.  Accepts either a numeric value interpreted
        as GiB (``HardDrive(32)`` → 32 GiB) or a human-readable string
        (``HardDrive("64GB")``, ``HardDrive("512M")``, ``HardDrive("1T")``).
        Defaults to ``"20GB"``.
    :param bus: Disk bus.  One of ``"virtio"``, ``"sata"``, ``"nvme"``,
        ``"scsi"``, ``"ide"``.  ``None`` (the default) lets the backend
        pick — typically ``"virtio"`` for Linux guests and ``"sata"``
        for Windows (driver-free install).
    :param nvme: Shortcut for ``bus="nvme"``.  Kept for ergonomics —
        ``HardDrive(2000, nvme=True)`` reads nicely.  ``bus=`` wins
        when both are set.

    Example::

        devices=[
            HardDrive(32),                # 32 GiB primary (OS) disk
            HardDrive(100),               # 100 GiB data disk
            HardDrive("2TB", nvme=True),  # 2 TB NVMe data disk
            HardDrive(500, bus="scsi"),   # 500 GiB virtio-scsi data disk
        ]
    """

    size: str
    """Normalised disk size string (e.g. ``'64GB'``, ``'32GiB'``)."""

    bus: str | None
    """Explicit bus selector, or ``None`` to let the backend default."""

    def __init__(
        self,
        size: int | float | str = "20GB",
        nvme: bool = False,
        bus: str | None = None,
    ) -> None:
        # Numeric sizes are interpreted as GiB for ergonomics — most
        # disk sizes in practice are whole-GiB counts, and it reads
        # naturally: ``HardDrive(32)`` rather than ``HardDrive("32GiB")``.
        if isinstance(size, (int, float)):
            if size <= 0:
                raise ValueError(f"HardDrive size must be > 0 GiB, got {size}")
            size = f"{size}GiB"
        # Validate size at construction time to give early feedback.
        parse_size(size)
        self.size = size

        # Resolve bus: explicit wins; else fall back to the nvme
        # shortcut; else ``None`` (backend decides).
        if bus is not None:
            if bus not in _HARD_DRIVE_BUSES:
                raise ValueError(
                    f"HardDrive bus={bus!r} is not one of "
                    f"{_HARD_DRIVE_BUSES}"
                )
            self.bus = bus
        elif nvme:
            self.bus = "nvme"
        else:
            self.bus = None

    @property
    def nvme(self) -> bool:
        """``True`` iff :attr:`bus` is ``"nvme"``.

        Kept for backward compatibility with call sites that want a
        quick boolean check instead of string comparison.
        """
        return self.bus == "nvme"

    @property
    def size_bytes(self) -> int:
        """Return the requested size in bytes.

        :returns: Size in bytes.
        """
        return parse_size(self.size)

    @property
    def qemu_size(self) -> str:
        """Return the size string in the canonical ``<integer>G`` form
        backends feed to their disk-sizing tools (e.g. ``'64G'``).

        :returns: Normalised size string.
        """
        return normalise_qemu_size(self.size)

    def resolved_bus(self, *, windows: bool = False) -> str:
        """Return the bus the backend should render for this drive.

        :param windows: ``True`` when the guest is Windows — flips
            the implicit default from virtio to SATA so Setup can
            see the disk without virtio-blk drivers.
        :returns: One of :data:`_HARD_DRIVE_BUSES`.
        """
        if self.bus is not None:
            return self.bus
        return "sata" if windows else "virtio"

    @property
    def device_type(self) -> str:
        """Return ``'harddrive'``.

        :returns: The string ``'harddrive'``.
        """
        return "harddrive"

    def __repr__(self) -> str:
        if self.bus == "nvme":
            return f"HardDrive({self.size!r}, nvme=True)"
        if self.bus is not None:
            return f"HardDrive({self.size!r}, bus={self.bus!r})"
        return f"HardDrive({self.size!r})"


class VirtualNetworkRef(AbstractDevice):
    """Attaches a VM to a named
    :class:`~testrange.networks.base.AbstractVirtualNetwork`.

    A VM can have multiple ``VirtualNetworkRef`` entries in its ``devices``
    list.  Each entry results in one virtual NIC on the VM.

    :param name: The ``name`` of the network to attach to.  Must match
        a network declared in the orchestrator's ``networks=`` list.
    :param ip: An optional static IPv4 address to assign to this NIC (e.g.
        ``'10.0.100.55'``).  If ``None`` (the default), the address is
        obtained via DHCP or a deterministic reservation.

    Example::

        VirtualNetworkRef("OfflineNet", ip="10.0.100.55")
        VirtualNetworkRef("NetA")   # DHCP
    """

    name: str
    """Name of the network this NIC attaches to."""

    ip: str | None
    """Optional static IPv4 address for this NIC; ``None`` means DHCP."""

    def __init__(self, name: str, ip: str | None = None) -> None:
        self.name = name
        self.ip = ip

    @property
    def device_type(self) -> str:
        """Return ``'network_ref'``.

        :returns: The string ``'network_ref'``.
        """
        return "network_ref"

    def __repr__(self) -> str:
        if self.ip:
            return f"VirtualNetworkRef({self.name!r}, ip={self.ip!r})"
        return f"VirtualNetworkRef({self.name!r})"


__all__ = [
    "AbstractDevice",
    "vCPU",
    "Memory",
    "HardDrive",
    "VirtualNetworkRef",
    "parse_size",
    "normalise_qemu_size",
]
