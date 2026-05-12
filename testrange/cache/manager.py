"""CacheManager — orchestrates lookups across cache tiers."""

from __future__ import annotations

from pathlib import Path

from testrange.cache.entry import CacheEntry
from testrange.cache.local import CacheEntryInfo, LocalCache


class CacheManager:
    """Read/write orchestration. Local tier only today."""

    def __init__(self, local: LocalCache | None = None) -> None:
        self.local = local or LocalCache()

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
