"""Pretty-name validator shared by the local + HTTP cache tiers.

Names land in two places: the ``names`` array of a sidecar, and (on the
HTTP tier) as path components under ``/names/<name>``. The HTTP exposure
means we must reject path-traversal and any other character that breaks
URL path semantics — so the local tier picks up the same rule for
symmetry.
"""

from __future__ import annotations

import re

from testrange.exceptions import CacheError

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


def validate_name(name: str) -> None:
    """Reject names that aren't safe to use as HTTP path components."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise CacheError(
            f"cache name {name!r} must match {_NAME_RE.pattern} (no slashes, spaces, or unicode)"
        )
