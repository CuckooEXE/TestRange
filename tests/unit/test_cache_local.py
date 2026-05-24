"""Tests for LocalCache."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.cache.local import default_root
from testrange.exceptions import CacheError, CacheMissError


def _make_blob(p: Path, payload: bytes = b"hello world\n") -> Path:
    p.write_bytes(payload)
    return p


class TestDefaultRoot:
    def test_xdg_cache_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        r = default_root()
        assert r == tmp_path / "testrange"

    def test_fallback_to_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        r = default_root()
        assert str(r).endswith("/.cache/testrange")


class TestStaging:
    def test_staging_is_on_cache_filesystem(self, tmp_path: Path) -> None:
        # CORE-4: staging must be a sibling of isos/ under the cache root, so
        # large captures stage on the same filesystem (not a small tmpfs /tmp).
        cache = LocalCache(root=tmp_path / "c")
        assert cache.staging.parent == cache.root
        assert cache.staging != cache.isos

    def test_staging_created_on_access(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        assert cache.staging.is_dir()


class TestLocalCacheAdd:
    def test_add_local_file(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src)
        assert info.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
        assert info.size == src.stat().st_size
        assert (cache.isos / f"{info.sha256}.bin").exists()
        assert (cache.isos / f"{info.sha256}.json").exists()

    def test_add_with_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src, name="my-base", description="hello")
        assert info.names == ("my-base",)
        assert info.description == "hello"

    def test_duplicate_add_dedupes(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        a = cache.add(src, name="alpha")
        b = cache.add(src, name="beta")
        assert a.sha256 == b.sha256
        assert set(b.names) == {"alpha", "beta"}
        assert len(list(cache.iter_entries())) == 1

    def test_add_nonexistent(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        with pytest.raises(CacheError, match="not a file"):
            cache.add(tmp_path / "nope.bin")

    def test_atomic_write_leaves_no_partials(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        cache.add(src)
        partials = list(cache.isos.glob("*.partial"))
        assert partials == []

    def test_sidecar_schema(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src, name="x")
        sidecar = json.loads((cache.isos / f"{info.sha256}.json").read_text())
        assert sidecar["sha256"] == info.sha256
        assert sidecar["names"] == ["x"]
        assert sidecar["size"] == info.size
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", sidecar["added_at"])


class TestLocalCacheResolve:
    def test_by_sha_full(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src)
        assert cache.resolve(info.sha256).sha256 == info.sha256

    def test_by_sha_prefix(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src)
        got = cache.resolve(info.sha256[:16])
        assert got.sha256 == info.sha256

    def test_by_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        cache.add(src, name="debian-13")
        assert cache.resolve("debian-13").names == ("debian-13",)

    def test_miss_by_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        with pytest.raises(CacheMissError):
            cache.resolve("nope")

    def test_miss_by_sha(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        with pytest.raises(CacheMissError):
            cache.resolve("a" * 32)


class TestLocalCacheMutate:
    def test_delete(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src, name="x")
        cache.delete("x")
        with pytest.raises(CacheMissError):
            cache.resolve(info.sha256)

    def test_add_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        cache.add(src, name="first")
        info = cache.add_name("first", "second")
        assert set(info.names) == {"first", "second"}

    def test_add_name_collision(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        a = _make_blob(tmp_path / "a.bin", b"a")
        b = _make_blob(tmp_path / "b.bin", b"b")
        cache.add(a, name="alpha")
        cache.add(b, name="beta")
        with pytest.raises(CacheError, match="already belongs"):
            cache.add_name("beta", "alpha")

    def test_forget_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        cache.add(src, name="x")
        info = cache.forget_name("x")
        assert info.names == ()
        with pytest.raises(CacheMissError):
            cache.resolve("x")

    def test_forget_unknown_name(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        with pytest.raises(CacheMissError):
            cache.forget_name("nope")


class TestCacheManager:
    def test_resolve_via_entry(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        src = _make_blob(tmp_path / "src.bin")
        info = cache.add(src, name="debian-13")
        mgr = CacheManager(local=cache)
        assert mgr.resolve(CacheEntry("debian-13")).sha256 == info.sha256
        assert mgr.resolve_path(CacheEntry("debian-13")) == info.path
