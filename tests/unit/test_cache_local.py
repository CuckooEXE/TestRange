"""Tests for LocalCache."""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.cache._names import validate_name
from testrange.cache.local import CacheEntryInfo, default_root
from testrange.exceptions import CacheError, CacheMissError


def _make_blob(p: Path, payload: bytes = b"hello world\n") -> Path:
    p.write_bytes(payload)
    return p


class _SlowResponse:
    """A urlopen() stand-in that dribbles bytes out, forcing concurrent
    downloads to interleave so a shared temp path would corrupt."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def __enter__(self) -> _SlowResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._buf.close()

    def read(self, size: int = -1) -> bytes:
        time.sleep(0.01)  # widen the interleave window
        return self._buf.read(size)


class TestConcurrentDownload:
    """Two concurrent URL adds must not collide on a shared ``.partial`` path.

    Before CACHE-4 both fetches streamed into a fixed ``.download.partial``;
    under the I/O phases' thread pool that interleaves into one file and
    promotes a disk whose bytes don't hash to its content-addressed name. With
    per-fetch ``mkstemp`` each download is isolated.
    """

    def test_two_url_adds_in_parallel_land_correctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bodies = {
            "http://x/a": b"A" * 4096,
            "http://x/b": b"B" * 8192,
        }

        def fake_urlopen(url: str, *_a: object, **_k: object) -> _SlowResponse:
            return _SlowResponse(bodies[url])

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        cache = LocalCache(root=tmp_path / "c")

        results: dict[str, CacheEntryInfo] = {}
        results_lock = threading.Lock()
        barrier = threading.Barrier(len(bodies))

        def add(url: str) -> None:
            barrier.wait()
            info = cache.add(url)
            with results_lock:
                results[url] = info

        threads = [threading.Thread(target=add, args=(u,)) for u in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for url, body in bodies.items():
            assert results[url].sha256 == hashlib.sha256(body).hexdigest()
            path = results[url].path
            assert path is not None
            assert path.read_bytes() == body
        # No stray temp files left behind under isos/.
        assert not list((cache.isos).glob("*.download.partial"))

    def test_same_content_different_names_merge(self, tmp_path: Path) -> None:
        # Two concurrent adds of byte-identical content under different names
        # (a parallel build capturing identical disks) must land one entry that
        # carries *both* aliases — no clobbered name, no crash on the shared
        # ``<sha>.json`` staging path.
        cache = LocalCache(root=tmp_path / "c")
        payload = b"IDENTICAL" * 500
        names = [f"_built_{i:02d}__os" for i in range(8)]
        srcs = [_make_blob(tmp_path / f"src{i}.bin", payload=payload) for i in range(8)]
        barrier = threading.Barrier(len(names))

        def add(i: int) -> None:
            barrier.wait()
            cache.add(srcs[i], name=names[i])

        threads = [threading.Thread(target=add, args=(i,)) for i in range(len(names))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = cache.list_entries()
        assert len(entries) == 1  # one content sha
        assert set(entries[0].names) == set(names)  # every alias survived the RMW


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


class TestResolveAmbiguity:
    def test_ambiguous_sha_prefix_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A short prefix matching two entries must raise, not silently return the
        # first sha-sorted match. Two shas sharing a 16-char prefix are injected.
        cache = LocalCache(root=tmp_path / "c")
        prefix = "a" * 16
        infos = [
            CacheEntryInfo(prefix + "b" * 48, 1, (), None, "2026-05-11T00:00:00Z", None, None),
            CacheEntryInfo(prefix + "c" * 48, 1, (), None, "2026-05-11T00:00:00Z", None, None),
        ]
        monkeypatch.setattr(cache, "iter_entries", lambda: iter(infos))
        with pytest.raises(CacheError, match="ambiguous"):
            cache.resolve(prefix)


class TestNameValidation:
    def test_all_dots_names_rejected(self) -> None:
        # "." / ".." pass the charset but are reserved path components on the
        # HTTP tier (/names/.. resolves to a parent dir).
        for bad in (".", "..", "..."):
            with pytest.raises(CacheError, match="all dots"):
                validate_name(bad)

    def test_normal_names_accepted(self) -> None:
        for ok in ("debian-13", "a.b", "v1.2.3", "x_y"):
            validate_name(ok)  # no raise


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

    def test_torn_write_preserves_committed_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Single-instance crash safety (ADR-0018): a failed atomic rename never
        # corrupts an already-committed entry — the canonical .bin is untouched.
        cache = LocalCache(root=tmp_path / "c")
        info = cache.add(_make_blob(tmp_path / "src.bin", payload=b"first\n"))
        bin_path = cache.isos / f"{info.sha256}.bin"
        committed = bin_path.read_bytes()

        def _boom(self: Path, target: object) -> None:
            raise OSError("simulated crash before rename completes")

        monkeypatch.setattr(Path, "replace", _boom)
        payload2 = b"second-different\n"
        sha2 = hashlib.sha256(payload2).hexdigest()
        with pytest.raises(OSError):
            cache.add(_make_blob(tmp_path / "src2.bin", payload=payload2))
        # The torn entry never appears at its canonical path (no corrupt hit)…
        assert not (cache.isos / f"{sha2}.bin").exists()
        # …and the previously-committed entry is intact.
        assert bin_path.read_bytes() == committed

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

    def test_purge_removes_every_entry(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        a = _make_blob(tmp_path / "a.bin", b"a")
        b = _make_blob(tmp_path / "b.bin", b"b")
        cache.add(a, name="alpha")
        cache.add(b, name="beta")
        removed = cache.purge()
        assert {i.sha256 for i in removed} == {
            hashlib.sha256(b"a").hexdigest(),
            hashlib.sha256(b"b").hexdigest(),
        }
        assert cache.list_entries() == []
        assert not any((tmp_path / "c" / "isos").glob("*.bin"))
        assert not any((tmp_path / "c" / "isos").glob("*.json"))

    def test_purge_empty_returns_nothing(self, tmp_path: Path) -> None:
        cache = LocalCache(root=tmp_path / "c")
        assert cache.purge() == []

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
