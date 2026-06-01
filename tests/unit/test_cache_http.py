"""Tests for HttpCache. ``requests`` is mocked end-to-end — no real sockets."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from testrange.cache.http import HttpCache
from testrange.cache.local import CacheEntryInfo
from testrange.exceptions import CacheError, CacheMissError


def _info(
    sha: str = "a" * 64,
    *,
    names: tuple[str, ...] = (),
    size: int = 12,
) -> CacheEntryInfo:
    return CacheEntryInfo(
        sha256=sha,
        size=size,
        names=names,
        origin=None,
        added_at="2026-05-11T00:00:00Z",
        description=None,
        path=None,
    )


def _resp(status: int, *, text: str = "", body: bytes = b"") -> MagicMock:
    """Build a ``requests.Response``-like mock."""
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.text = text
    r.content = body
    r.iter_content = lambda chunk_size=None: iter([body]) if body else iter([])
    return r


@pytest.fixture
def fake_requests() -> Any:
    """Patch ``HttpCache``'s lazy import to return a fake ``requests`` module."""
    fake = MagicMock(name="requests")
    with patch("testrange.cache.http._import_requests", return_value=fake):
        yield fake


class TestConstruction:
    def test_strips_trailing_slash(self) -> None:
        c = HttpCache("https://cache.local:8443/")
        assert c.base_url == "https://cache.local:8443"

    def test_rejects_empty(self) -> None:
        with pytest.raises(CacheError, match="non-empty"):
            HttpCache("")


class TestResolve:
    def test_by_sha_hit(self, fake_requests: Any) -> None:
        sidecar = {
            "sha256": "a" * 64,
            "size": 42,
            "names": ["debian-13"],
            "origin": None,
            "added_at": "2026-05-11T00:00:00Z",
            "description": None,
        }
        fake_requests.get.return_value = _resp(200, text=json.dumps(sidecar))
        c = HttpCache("https://h")
        info = c.resolve("a" * 64)
        assert info.sha256 == "a" * 64
        assert info.size == 42
        assert info.names == ("debian-13",)
        assert info.path is None
        # One GET to /isos/<sha>.json — no /names/<n> lookup needed for a
        # full sha.
        assert fake_requests.get.call_count == 1
        url = fake_requests.get.call_args.args[0]
        assert url.endswith(f"/isos/{'a' * 64}.json")

    def test_by_name_hit(self, fake_requests: Any) -> None:
        sidecar = {
            "sha256": "b" * 64,
            "size": 7,
            "names": ["thing"],
            "origin": None,
            "added_at": "2026-05-11T00:00:00Z",
            "description": None,
        }
        fake_requests.get.side_effect = [
            _resp(200, text="b" * 64),  # /names/thing
            _resp(200, text=json.dumps(sidecar)),  # /isos/<sha>.json
        ]
        c = HttpCache("https://h")
        info = c.resolve("thing")
        assert info.sha256 == "b" * 64
        assert info.names == ("thing",)
        assert fake_requests.get.call_count == 2

    def test_name_404_raises_miss(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(404)
        c = HttpCache("https://h")
        with pytest.raises(CacheMissError, match="thing"):
            c.resolve("thing")

    def test_name_resolved_to_empty_sha_is_error(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(200, text="\n")
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="empty sha"):
            c.resolve("thing")

    def test_5xx_on_name_is_cache_error(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(503)
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="503"):
            c.resolve("thing")

    def test_sidecar_404_is_miss(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(404)
        c = HttpCache("https://h")
        with pytest.raises(CacheMissError):
            c.resolve("a" * 64)


class TestFetch:
    def test_streams_into_dest(self, fake_requests: Any, tmp_path: Path) -> None:
        payload = b"PAYLOAD"
        sha = hashlib.sha256(payload).hexdigest()
        fake_requests.get.return_value = _resp(200, body=payload)
        c = HttpCache("https://h")
        dest = tmp_path / "out.bin"
        c.fetch(sha, dest)
        assert dest.read_bytes() == payload
        assert fake_requests.get.call_args.kwargs["stream"] is True
        assert list(tmp_path.glob("*.partial")) == []  # temp promoted, not left behind

    def test_fetched_bin_is_world_readable(self, fake_requests: Any, tmp_path: Path) -> None:
        # CACHE-7: the fetched .bin lands at 0644, not mkstemp's 0600, matching
        # locally-added entries in the same content-addressed store.
        payload = b"PAYLOAD"
        sha = hashlib.sha256(payload).hexdigest()
        fake_requests.get.return_value = _resp(200, body=payload)
        c = HttpCache("https://h")
        dest = tmp_path / "out.bin"
        c.fetch(sha, dest)
        assert stat.S_IMODE(dest.stat().st_mode) == 0o644

    def test_sha_mismatch_rejected_and_no_residue(self, fake_requests: Any, tmp_path: Path) -> None:
        # B3: a body that doesn't hash to the requested sha (corruption, or a
        # swapped payload over unverified TLS) is rejected — never landed at the
        # canonical path, and no .partial residue masquerades as a valid hit.
        fake_requests.get.return_value = _resp(200, body=b"WRONG-BYTES")
        c = HttpCache("https://h")
        dest = tmp_path / "out.bin"
        with pytest.raises(CacheError, match="hash"):
            c.fetch("a" * 64, dest)
        assert not dest.exists()
        assert list(tmp_path.glob("*.partial")) == []

    def test_404_raises_miss(self, fake_requests: Any, tmp_path: Path) -> None:
        fake_requests.get.return_value = _resp(404)
        c = HttpCache("https://h")
        with pytest.raises(CacheMissError):
            c.fetch("a" * 64, tmp_path / "x")

    def test_5xx_raises_cache_error(self, fake_requests: Any, tmp_path: Path) -> None:
        fake_requests.get.return_value = _resp(500)
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="500"):
            c.fetch("a" * 64, tmp_path / "x")


class TestPushOrdering:
    def test_bin_then_sidecar_then_names(self, fake_requests: Any, tmp_path: Path) -> None:
        # Every PUT returns 201 Created.
        fake_requests.put.return_value = _resp(201)
        bin_path = tmp_path / "blob.bin"
        bin_path.write_bytes(b"X" * 10)
        info = _info(sha="c" * 64, names=("alpha", "beta"))
        c = HttpCache("https://h")
        c.push(info, bin_path)

        # Four PUTs in order: bin, sidecar, /names/alpha, /names/beta.
        urls = [call.args[0] for call in fake_requests.put.call_args_list]
        assert urls == [
            f"https://h/isos/{'c' * 64}.bin",
            f"https://h/isos/{'c' * 64}.json",
            "https://h/names/alpha",
            "https://h/names/beta",
        ]
        # Bin PUT carries explicit Content-Length so requests doesn't
        # buffer the whole qcow2 in memory.
        bin_call = fake_requests.put.call_args_list[0]
        assert bin_call.kwargs["headers"]["Content-Length"] == "10"

    def test_bin_put_failure_aborts(self, fake_requests: Any, tmp_path: Path) -> None:
        fake_requests.put.return_value = _resp(500)
        bin_path = tmp_path / "blob.bin"
        bin_path.write_bytes(b"X")
        info = _info()
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="500"):
            c.push(info, bin_path)
        # Only the bin attempt was made — no sidecar, no names.
        assert fake_requests.put.call_count == 1


class TestDeleteOrdering:
    def test_names_then_sidecar_then_bin(self, fake_requests: Any) -> None:
        fake_requests.delete.return_value = _resp(204)
        info = _info(sha="d" * 64, names=("foo", "bar"))
        c = HttpCache("https://h")
        c.delete(info)
        urls = [call.args[0] for call in fake_requests.delete.call_args_list]
        assert urls == [
            "https://h/names/foo",
            "https://h/names/bar",
            f"https://h/isos/{'d' * 64}.json",
            f"https://h/isos/{'d' * 64}.bin",
        ]

    def test_404_on_delete_is_quietly_accepted(self, fake_requests: Any) -> None:
        fake_requests.delete.return_value = _resp(404)
        info = _info(sha="d" * 64, names=("foo",))
        c = HttpCache("https://h")
        c.delete(info)  # no raise

    def test_5xx_on_delete_raises(self, fake_requests: Any) -> None:
        fake_requests.delete.return_value = _resp(500)
        info = _info(sha="d" * 64, names=("foo",))
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="500"):
            c.delete(info)


class TestAddName:
    def test_rewrites_sidecar_then_puts_name(self, fake_requests: Any) -> None:
        existing = {
            "sha256": "e" * 64,
            "size": 7,
            "names": [],
            "origin": None,
            "added_at": "2026-05-11T00:00:00Z",
            "description": None,
        }
        fake_requests.get.return_value = _resp(200, text=json.dumps(existing))
        fake_requests.put.return_value = _resp(201)
        c = HttpCache("https://h")
        c.add_name("e" * 64, "thing")
        urls = [call.args[0] for call in fake_requests.put.call_args_list]
        assert urls == [
            f"https://h/isos/{'e' * 64}.json",  # sidecar first
            "https://h/names/thing",  # pointer second
        ]
        body = fake_requests.put.call_args_list[0].kwargs["data"]
        body_text = body.decode("utf-8") if isinstance(body, bytes) else body
        assert "thing" in body_text

    def test_rejects_bad_name(self, fake_requests: Any) -> None:
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="must match"):
            c.add_name("e" * 64, "bad/name")

    def test_missing_sha_raises(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(404)
        c = HttpCache("https://h")
        with pytest.raises(CacheError, match="not present"):
            c.add_name("e" * 64, "thing")


class TestForgetName:
    def test_drops_pointer_then_rewrites_sidecar(self, fake_requests: Any) -> None:
        sidecar = {
            "sha256": "f" * 64,
            "size": 7,
            "names": ["thing", "other"],
            "origin": None,
            "added_at": "2026-05-11T00:00:00Z",
            "description": None,
        }
        fake_requests.get.side_effect = [
            _resp(200, text="f" * 64),  # /names/thing
            _resp(200, text=json.dumps(sidecar)),  # /isos/<sha>.json
        ]
        fake_requests.delete.return_value = _resp(204)
        fake_requests.put.return_value = _resp(201)
        c = HttpCache("https://h")
        c.forget_name("thing")
        # Order: DELETE pointer, then PUT updated sidecar without "thing".
        assert fake_requests.delete.call_args_list[0].args[0] == "https://h/names/thing"
        put_body = fake_requests.put.call_args.kwargs["data"]
        body_text = put_body.decode("utf-8") if isinstance(put_body, bytes) else put_body
        assert "thing" not in json.loads(body_text)["names"]
        assert "other" in json.loads(body_text)["names"]

    def test_already_absent_is_noop(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(404)
        c = HttpCache("https://h")
        c.forget_name("thing")  # no raise
        assert fake_requests.delete.call_count == 0
        assert fake_requests.put.call_count == 0


class TestVerifyAlwaysFalse:
    def test_get_passes_verify_false(self, fake_requests: Any) -> None:
        fake_requests.get.return_value = _resp(
            200,
            text=json.dumps(
                {
                    "sha256": "a" * 64,
                    "size": 1,
                    "names": [],
                    "origin": None,
                    "added_at": "x",
                    "description": None,
                }
            ),
        )
        c = HttpCache("https://h")
        c.resolve("a" * 64)
        # The whole point of this cache is to trust the network gate
        # instead of TLS identity. verify=False on every request, no toggle.
        assert fake_requests.get.call_args.kwargs["verify"] is False

    def test_put_passes_verify_false(self, fake_requests: Any, tmp_path: Path) -> None:
        fake_requests.put.return_value = _resp(201)
        bin_path = tmp_path / "x"
        bin_path.write_bytes(b"y")
        c = HttpCache("https://h")
        c.push(_info(), bin_path)
        for call in fake_requests.put.call_args_list:
            assert call.kwargs["verify"] is False

    def test_delete_passes_verify_false(self, fake_requests: Any) -> None:
        fake_requests.delete.return_value = _resp(204)
        c = HttpCache("https://h")
        c.delete(_info(sha="a" * 64, names=()))
        for call in fake_requests.delete.call_args_list:
            assert call.kwargs["verify"] is False
