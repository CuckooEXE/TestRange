"""Size-string parsing helpers shared by device definitions.

Separate from any specific device so that
:class:`~testrange.devices.HardDrive` and any future size-carrying
device can reuse the same parser without importing each other.
"""

from __future__ import annotations

import re

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


def normalise_size(size: str) -> str:
    """Return the size string in the canonical ``<integer>G`` form used
    by every shipped backend's disk-sizing tools.

    Converts to the nearest GiB integer with a ``G`` suffix.  This
    form is widely accepted (libvirt's ``qemu-img``, proxmox storage
    tools, Hyper-V's ``New-VHD``, …); backends that need a different
    syntax can re-parse the canonical string.

    :param size: Human-readable size string.
    :returns: String like ``'64G'``.
    """
    gib = parse_size(size) // (1024 ** 3)
    return f"{max(gib, 1)}G"


__all__ = ["parse_size", "normalise_size"]
