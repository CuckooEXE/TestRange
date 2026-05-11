"""CacheManager — composes LocalCache (always) and HttpCache (optional, deferred)."""

from __future__ import annotations

from pathlib import Path

from testrange.cache.entry import CacheEntry
from testrange.cache.http import HttpCache
from testrange.cache.local import CacheEntryInfo, LocalCache


class CacheManager:
    """Read/write orchestration across cache tiers.

    Phase 1: local tier only. Phase-N: HTTP tier wired here too (read:
    local -> HTTP -> miss; write: local always, HTTP best-effort).
    """

    def __init__(
        self,
        local: LocalCache | None = None,
        http: HttpCache | None = None,
    ) -> None:
        self.local = local or LocalCache()
        self.http = http

    @property
    def root(self) -> Path:
        return self.local.root

    def resolve(self, ref: str | CacheEntry) -> CacheEntryInfo:
        """Resolve a CacheEntry (or raw identifier string) to its info."""
        identifier = ref.identifier if isinstance(ref, CacheEntry) else ref
        return self.local.resolve(identifier)

    def resolve_path(self, ref: str | CacheEntry) -> Path:
        """Convenience: return the on-disk .bin path."""
        return self.resolve(ref).path

    def attach_http(self, url: str) -> None:
        """Inject an HTTP cache after construction (CLI --cache plumbing)."""
        self.http = HttpCache(url)
