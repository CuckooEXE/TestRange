"""HTTP cache (second tier) — deferred to a later phase.

Plan-level reference only in v0. The CLI accepts ``--cache https://…`` and
stores the URL on the manager, but no HTTP I/O happens yet. See TODO.md.
"""

from __future__ import annotations

from testrange.exceptions import CacheError


class HttpCache:
    """Stub for the HTTP second-tier cache."""

    def __init__(self, base_url: str) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise CacheError(f"HttpCache base_url must be http(s)://; got {base_url!r}")
        self.base_url = base_url.rstrip("/")
