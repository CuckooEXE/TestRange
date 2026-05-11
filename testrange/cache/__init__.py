"""Cache layer for testrange.

Public surface: ``CacheEntry``. Phase 0 holds the data type; Phase 1 wires
the local cache implementation.
"""

from __future__ import annotations

from testrange.cache.entry import CacheEntry

__all__ = ["CacheEntry"]
