"""Tests for CacheEntry Plan-time data type."""

from __future__ import annotations

import pytest

from testrange.cache import CacheEntry


class TestCacheEntry:
    def test_pretty_name(self) -> None:
        e = CacheEntry("debian-13")
        assert e.identifier == "debian-13"
        assert e.looks_like_sha is False

    def test_sha_16(self) -> None:
        e = CacheEntry("abc1234567890def")
        assert e.looks_like_sha is True

    def test_sha_64(self) -> None:
        e = CacheEntry("a" * 64)
        assert e.looks_like_sha is True

    def test_too_short_for_sha(self) -> None:
        e = CacheEntry("abc12345")
        assert e.looks_like_sha is False

    def test_empty(self) -> None:
        with pytest.raises(ValueError):
            CacheEntry("")

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        e = CacheEntry("x")
        with pytest.raises(FrozenInstanceError):
            e.identifier = "y"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert "name='debian-13'" in repr(CacheEntry("debian-13"))
        assert "sha=" in repr(CacheEntry("a" * 32))
