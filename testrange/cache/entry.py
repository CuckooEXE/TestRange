"""``CacheEntry`` — a Plan-time reference to a cached artifact.

A user writes ``CacheEntry("debian-13")`` or ``CacheEntry("abc123def...")``.
The single positional string is auto-detected at resolution time:
  - matches ``^[0-9a-f]{16,64}$`` -> treated as a content sha;
  - otherwise treated as a pretty-name (resolved via the cache index).

Plan-time `CacheEntry` is pure data. Resolution lives on the CacheManager.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX_PAT = re.compile(r"^[0-9a-f]{16,64}$")


@dataclass(frozen=True)
class CacheEntry:
    """Reference to a cached artifact, by content sha or pretty-name."""

    identifier: str

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ValueError("CacheEntry identifier must be a non-empty string")

    @property
    def looks_like_sha(self) -> bool:
        """True iff identifier looks like a hex digest (16-64 lowercase hex chars)."""
        return bool(_HEX_PAT.match(self.identifier))

    def __repr__(self) -> str:
        kind = "sha" if self.looks_like_sha else "name"
        return f"CacheEntry({kind}={self.identifier!r})"
