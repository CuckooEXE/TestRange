"""OS image resolution utilities.

:func:`resolve_image` accepts either an absolute local path or an
``https://`` URL.  The cache stores whatever extension the URL has
verbatim — ``.qcow2``, ``.img``, ``.iso``, ``.vhdx`` are all fine
inputs; the backend's builder decides what to do with the bytes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from testrange.exceptions import ImageNotFoundError

if TYPE_CHECKING:
    from testrange.cache import CacheManager


def resolve_image(iso: str, cache: CacheManager) -> Path:
    """Resolve the ``iso=`` parameter of a VM to a local image path.

    Resolution order:

    1. If *iso* is an absolute path (or ``~/``-prefixed path) that exists on
       disk, return it directly without downloading anything.
    2. If *iso* starts with ``https://``, download and cache the image via
       :meth:`~testrange.cache.CacheManager.get_image`.
    3. Otherwise raise :class:`~testrange.exceptions.ImageNotFoundError`.

    :param iso: An absolute local path or an ``https://`` URL pointing
        to an image file.  Format is up to the backend's builder; the
        cache stores whatever extension the URL has.
    :param cache: An active :class:`~testrange.cache.CacheManager` instance.
    :returns: Absolute path to the local image file.
    :raises ImageNotFoundError: If *iso* is neither a valid local path nor
        an ``https://`` URL.
    """
    local = Path(iso)
    if local.is_absolute() and local.exists():
        return local

    expanded = Path(os.path.expanduser(iso))
    if expanded.exists():
        return expanded

    if iso.startswith("https://"):
        return cache.get_image(iso)

    raise ImageNotFoundError(
        f"Cannot resolve image {iso!r}. "
        "Pass an absolute path to a local image file, "
        "or an https:// URL."
    )


def is_windows_image(iso: str) -> bool:
    """Return ``True`` if the image string looks like a Windows ISO.

    Checks common Windows ISO filename patterns while avoiding the
    Linux-server false-positives that bare-substring matches would
    cause.  Specifically:

    * Bare ``"server"`` would match ``ubuntu-22.04-live-server-amd64.iso``
      and ``debian-12-server.iso`` — Linux ISOs that would silently
      fall into the Windows builder path.  We require ``server`` to
      be followed by a 4-digit year (``server-2019``, ``server2022``)
      so the Microsoft-style names match but ``server-amd64`` /
      ``server-22.04`` don't.
    * Bare ``"win"`` would match anything with ``win`` as a substring
      (``winetricks``, etc.); we use a regex anchor that requires
      a digit immediately after ``w`` / ``win`` so ``win10`` / ``win11``
      / ``w10`` match but ``winetricks`` doesn't.
    * ``"windows"`` and ``"ltsc"`` are unambiguous tokens used in
      Microsoft's own filenames; substring match is safe.

    :param iso: The ``iso=`` string to inspect.
    :returns: ``True`` if the image appears to be a Windows ISO.
    """
    lower = iso.lower()
    if not lower.endswith(".iso"):
        return False
    if "windows" in lower or "ltsc" in lower:
        return True
    # win10 / win11 / w10 / w11 — require a digit after w / win so
    # `winetricks` and other ``win``-prefix words don't false-match.
    if re.search(r"\bw(?:in)?\d", lower):
        return True
    # Windows Server filenames carry a 4-digit year token
    # (``server2019``, ``server-2022``, ``server_2025``) — the year
    # disambiguates them from Linux ``-server-`` filenames whose
    # following token is the distro version (``ubuntu-server-22.04``)
    # or an architecture (``server-amd64``).
    if re.search(r"server[-_]?20\d{2}", lower):
        return True
    return False
