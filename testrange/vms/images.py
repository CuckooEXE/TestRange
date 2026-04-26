"""OS image resolution utilities.

:func:`resolve_image` accepts either an absolute local path or an
``https://`` URL.  The cache stores whatever extension the URL has
verbatim — ``.qcow2``, ``.img``, ``.iso``, ``.vhdx`` are all fine
inputs; the backend's builder decides what to do with the bytes.
"""

from __future__ import annotations

import os
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

    Checks for common Windows ISO filename patterns.

    :param iso: The ``iso=`` string to inspect.
    :returns: ``True`` if the image appears to be a Windows ISO.
    """
    lower = iso.lower()
    return any(
        kw in lower
        for kw in ("win", "windows", "server", "w10", "w11", "ltsc")
    ) and lower.endswith(".iso")
