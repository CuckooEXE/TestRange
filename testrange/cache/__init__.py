"""Cache layer for testrange.

Public surface: ``CacheEntry`` (Plan-time reference type) and the runtime
``CacheManager`` / ``LocalCache``.
"""

from __future__ import annotations

from testrange.cache.entry import CacheEntry
from testrange.cache.local import CacheEntryInfo, LocalCache, default_root
from testrange.cache.manager import CacheManager

__all__ = [
    "CacheEntry",
    "CacheEntryInfo",
    "CacheManager",
    "LocalCache",
    "default_root",
]
