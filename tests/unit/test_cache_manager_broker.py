"""Tests for CacheManager as a broker between local + http tiers.

The HttpCache is a stand-in mock — the wire-level behavior is covered in
``test_cache_http.py``. Here we care about the broker policy: fallthrough,
mirror, ordering, error tolerance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange.cache import CacheManager, HttpCache, LocalCache
from testrange.cache.local import CacheEntryInfo
from testrange.exceptions import CacheError, CacheMissError


def _local(tmp_path: Path) -> LocalCache:
    return LocalCache(root=tmp_path / "local")


def _make_blob(path: Path, payload: bytes = b"hello\n") -> Path:
    path.write_bytes(payload)
    return path


def _http_mock() -> MagicMock:
    """Build an HttpCache-shaped mock."""
    m = MagicMock(spec=HttpCache)
    return m


class TestNoHttpConfigured:
    """With ``http=None``, behavior must match v0.0.1 exactly."""

    def test_resolve_local_hit(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        src = _make_blob(tmp_path / "src.bin")
        info = local.add(src, name="thing")
        mgr = CacheManager(local=local)
        out = mgr.resolve("thing")
        assert out.sha256 == info.sha256
        assert out.path is not None

    def test_resolve_local_miss_raises(self, tmp_path: Path) -> None:
        mgr = CacheManager(local=_local(tmp_path))
        with pytest.raises(CacheMissError):
            mgr.resolve("nope")

    def test_add_no_http_call(self, tmp_path: Path) -> None:
        mgr = CacheManager(local=_local(tmp_path))
        src = _make_blob(tmp_path / "src.bin")
        info = mgr.add(src, name="thing")
        assert info.path is not None and info.path.exists()

    def test_push_pull_without_http_raise(self, tmp_path: Path) -> None:
        mgr = CacheManager(local=_local(tmp_path))
        with pytest.raises(CacheError, match="no HTTP cache configured"):
            mgr.push("nope")
        with pytest.raises(CacheError, match="no HTTP cache configured"):
            mgr.pull("nope")


class TestResolveFallthrough:
    def test_local_miss_http_hit_fetches_into_local(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()

        # Pretend the HTTP server has the entry.
        http_info = CacheEntryInfo(
            sha256="z" * 64,
            size=12,
            names=("debian-13",),
            origin=None,
            added_at="2026-05-11T00:00:00Z",
            description=None,
            path=None,
        )
        http.resolve.return_value = http_info

        # Simulate the fetch by writing the bin to the local path.
        def fake_fetch(sha: str, dest: Path) -> None:
            dest.write_bytes(b"PAYLOAD-FROM-HTTP")
        http.fetch.side_effect = fake_fetch

        mgr = CacheManager(local=local, http=http)
        out = mgr.resolve("debian-13")
        assert out.sha256 == "z" * 64
        assert out.path == local.isos / f"{'z' * 64}.bin"
        assert out.path.exists()
        # Sidecar was written LAST — verify it landed.
        assert (local.isos / f"{'z' * 64}.json").exists()

        # A second resolve should be a local hit — no extra HTTP traffic.
        http.resolve.reset_mock()
        http.fetch.reset_mock()
        out2 = mgr.resolve("debian-13")
        assert out2.sha256 == "z" * 64
        assert http.resolve.call_count == 0
        assert http.fetch.call_count == 0

    def test_local_miss_http_miss_raises(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        http.resolve.side_effect = CacheMissError("not in http either")
        mgr = CacheManager(local=local, http=http)
        with pytest.raises(CacheMissError):
            mgr.resolve("nope")

    def test_fetch_false_returns_http_info_without_materializing(
        self, tmp_path: Path
    ) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        http.resolve.return_value = CacheEntryInfo(
            sha256="z" * 64,
            size=5,
            names=("thing",),
            origin=None,
            added_at="2026-05-11T00:00:00Z",
            description=None,
            path=None,
        )
        mgr = CacheManager(local=local, http=http)
        out = mgr.resolve("thing", fetch=False)
        assert out.path is None
        # Crucially: no fetch, no local sidecar write.
        assert http.fetch.call_count == 0
        assert not (local.isos / f"{'z' * 64}.bin").exists()
        assert not (local.isos / f"{'z' * 64}.json").exists()


class TestAddMirrors:
    def test_add_mirrors_to_http(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin", b"DATA")
        info = mgr.add(src, name="thing")
        http.push.assert_called_once()
        # The mock was handed the local-flavored info + the bin path.
        push_info, push_path = http.push.call_args.args
        assert push_info.sha256 == info.sha256
        assert push_path == info.path

    def test_add_succeeds_even_when_http_push_fails(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        http.push.side_effect = CacheError("network down")
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin", b"DATA")
        # The contract is "best-effort mirror" — http.push raises but the
        # local op must still complete and a valid local entry must remain.
        # (caplog is brittle here because cli.main() in earlier tests sets
        # propagate=False on the testrange logger.)
        info = mgr.add(src, name="thing")
        assert info.path is not None and info.path.exists()
        assert (local.isos / f"{info.sha256}.json").exists()
        # And http.push WAS attempted (so we know the broker tried to
        # mirror — it just swallowed the failure).
        assert http.push.call_count == 1


class TestDeleteMirrors:
    def test_delete_mirrors_to_http(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin")
        mgr.add(src, name="thing")
        http.reset_mock()
        info = mgr.delete("thing")
        http.delete.assert_called_once()
        deleted_info: Any = http.delete.call_args.args[0]
        assert deleted_info.sha256 == info.sha256

    def test_delete_local_failure_skips_http(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        with pytest.raises(CacheMissError):
            mgr.delete("nope")
        assert http.delete.call_count == 0


class TestAddNameMirrors:
    def test_add_name_mirrors_to_http(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin")
        mgr.add(src, name="first")
        http.reset_mock()
        info = mgr.add_name("first", "second")
        http.add_name.assert_called_once_with(info.sha256, "second")

    def test_forget_name_mirrors_to_http(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin")
        mgr.add(src, name="thing")
        http.reset_mock()
        mgr.forget_name("thing")
        http.forget_name.assert_called_once_with("thing")


class TestPushPull:
    def test_push_invokes_http_push(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        mgr = CacheManager(local=local, http=http)
        src = _make_blob(tmp_path / "src.bin")
        info = mgr.add(src, name="thing")
        http.reset_mock()
        out = mgr.push("thing")
        http.push.assert_called_once()
        push_info, push_path = http.push.call_args.args
        assert push_info.sha256 == info.sha256
        assert push_path == info.path
        assert out.sha256 == info.sha256

    def test_pull_materializes_into_local(self, tmp_path: Path) -> None:
        local = _local(tmp_path)
        http = _http_mock()
        http.resolve.return_value = CacheEntryInfo(
            sha256="z" * 64,
            size=7,
            names=("debian-13",),
            origin=None,
            added_at="2026-05-11T00:00:00Z",
            description=None,
            path=None,
        )
        def fake_fetch(sha: str, dest: Path) -> None:
            dest.write_bytes(b"PAYLOAD")
        http.fetch.side_effect = fake_fetch
        mgr = CacheManager(local=local, http=http)
        info = mgr.pull("debian-13")
        assert info.path == local.isos / f"{'z' * 64}.bin"
        assert info.path.exists()
        assert (local.isos / f"{'z' * 64}.json").exists()
