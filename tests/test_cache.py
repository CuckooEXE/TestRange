"""Unit tests for :mod:`testrange.cache`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from testrange.cache import CacheManager, _sha256_file, vm_config_hash
from testrange.exceptions import ImageNotFoundError
from testrange.storage import LocalStorageBackend


def _local_backend(cache_root: Path) -> LocalStorageBackend:
    """Shortcut: backend rooted at *cache_root* — identical to what
    Orchestrator auto-selects for the default ``qemu:///system`` case."""
    return LocalStorageBackend(cache_root)


class TestVmConfigHash:
    def test_deterministic(self) -> None:
        h1 = vm_config_hash("debian-12", [("root", "pw", False)], ["Apt('nginx')"], [], "20G")
        h2 = vm_config_hash("debian-12", [("root", "pw", False)], ["Apt('nginx')"], [], "20G")
        assert h1 == h2

    def test_length_is_24(self) -> None:
        h = vm_config_hash("debian-12", [], [], [], "20G")
        assert len(h) == 24

    def test_lowercase_hex(self) -> None:
        h = vm_config_hash("debian-12", [], [], [], "20G")
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)

    def test_changes_with_iso(self) -> None:
        a = vm_config_hash("debian-12", [], [], [], "20G")
        b = vm_config_hash("debian-13", [], [], [], "20G")
        assert a != b

    def test_changes_with_disk_size(self) -> None:
        a = vm_config_hash("debian-12", [], [], [], "20G")
        b = vm_config_hash("debian-12", [], [], [], "40G")
        assert a != b

    def test_user_order_insensitive(self) -> None:
        a = vm_config_hash("x", [("root", "p", False), ("a", "p", False)], [], [], "20G")
        b = vm_config_hash("x", [("a", "p", False), ("root", "p", False)], [], [], "20G")
        assert a == b

    def test_package_order_insensitive(self) -> None:
        a = vm_config_hash("x", [], ["Apt('a')", "Apt('b')"], [], "20G")
        b = vm_config_hash("x", [], ["Apt('b')", "Apt('a')"], [], "20G")
        assert a == b

    def test_post_install_cmd_order_matters(self) -> None:
        """Regression: post_install_cmds are ordered, not a set."""
        a = vm_config_hash("x", [], [], ["cmd1", "cmd2"], "20G")
        b = vm_config_hash("x", [], [], ["cmd2", "cmd1"], "20G")
        assert a != b


class TestSha256File:
    def test_hash_of_file(self, tmp_path: Path) -> None:
        f = tmp_path / "x.bin"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert _sha256_file(f) == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert _sha256_file(f) == expected

    def test_hashes_large_file_in_blocks(self, tmp_path: Path) -> None:
        f = tmp_path / "big.bin"
        data = b"x" * (3 * 1024 * 1024)  # 3MB; forces multiple blocks
        f.write_bytes(data)
        assert _sha256_file(f) == hashlib.sha256(data).hexdigest()


class TestCacheManagerInit:
    def test_creates_subdirectories(self, tmp_cache_root: Path) -> None:
        CacheManager(root=tmp_cache_root)
        assert (tmp_cache_root / "images").is_dir()
        assert (tmp_cache_root / "vms").is_dir()

    def test_only_images_and_vms_at_root(self, tmp_cache_root: Path) -> None:
        """Regression: the cache directory must contain ONLY images/ and
        vms/.  Any other entry at the top level is a leak of ephemeral
        state that belongs in a run directory."""
        CacheManager(root=tmp_cache_root)
        entries = {p.name for p in tmp_cache_root.iterdir()}
        assert entries == {"images", "vms"}

    def test_idempotent(self, tmp_cache_root: Path) -> None:
        CacheManager(root=tmp_cache_root)
        CacheManager(root=tmp_cache_root)  # must not raise

    def test_root_property(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        assert c.root == tmp_cache_root
        assert c.images_dir == tmp_cache_root / "images"
        assert c.vms_dir == tmp_cache_root / "vms"
        # The removed `runs_dir` attribute must not come back.
        assert not hasattr(c, "runs_dir")


class TestGetVm:
    def test_cache_miss(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        assert c.get_vm("nonexistent", b) is None

    def test_cache_hit_flat_layout(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        disk = c.vms_dir / "abc123.qcow2"
        disk.write_bytes(b"fake")
        # For LocalStorageBackend, the ref is just the absolute path.
        assert c.get_vm("abc123", b) == str(disk)

    def test_vm_paths(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        assert (
            c.vm_snapshot_ref("abc", b)
            == str(tmp_cache_root / "vms" / "abc.qcow2")
        )
        assert (
            c.vm_manifest_ref("abc", b)
            == str(tmp_cache_root / "vms" / "abc.json")
        )


class TestStoreVm:
    def test_writes_flat_qcow2_and_json(
        self,
        tmp_cache_root: Path,
        fake_qemu_img: list[list[str]],
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "work.qcow2"
        src.write_bytes(b"x")
        manifest = {"name": "vm1", "iso": "debian-12"}

        dest_ref = c.store_vm("abc123", str(src), manifest, b)
        dest = Path(dest_ref)

        assert dest == tmp_cache_root / "vms" / "abc123.qcow2"
        assert (tmp_cache_root / "vms" / "abc123.json").exists()
        # No per-hash subdirectory anymore
        assert not (tmp_cache_root / "vms" / "abc123").exists()

    def test_manifest_contents(
        self,
        tmp_cache_root: Path,
        fake_qemu_img: list[list[str]],
    ) -> None:
        """The .json must be inspectable and record what built the image."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "work.qcow2"
        src.write_bytes(b"x")
        manifest = {
            "name": "vm1",
            "iso": "debian-12",
            "packages": ["Apt('nginx')"],
            "post_install_cmds": ["systemctl enable nginx"],
        }
        c.store_vm("abc123", str(src), manifest, b)

        written = json.loads(
            (tmp_cache_root / "vms" / "abc123.json").read_text()
        )
        assert written == manifest

    def test_store_vm_invokes_compressed_convert(
        self,
        tmp_cache_root: Path,
        fake_qemu_img: list[list[str]],
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "work.qcow2"
        src.write_bytes(b"x")
        c.store_vm("abc", str(src), {"name": "v"}, b)
        assert any(
            cmd[:5] == ["qemu-img", "convert", "-f", "qcow2", "-O"]
            and "-c" in cmd
            for cmd in fake_qemu_img
        )


class TestGetImage:
    def test_cache_hit_skips_download(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        url = "https://example.com/x.qcow2"
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
        img = c.images_dir / f"{url_hash}.qcow2"
        img.write_bytes(b"cached")
        meta = c.images_dir / f"{url_hash}.meta.json"
        meta.write_text(json.dumps({"url": url}))

        def _explode(*_a, **_k):
            raise AssertionError("should not download on cache hit")

        monkeypatch.setattr(c, "_download", _explode)
        assert c.get_image(url) == img

    def test_download_failure_wraps_in_image_not_found(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        c = CacheManager(root=tmp_cache_root)

        def _explode(*_a, **_k):
            raise requests.RequestException("network down")

        monkeypatch.setattr(c, "_download", _explode)
        with pytest.raises(ImageNotFoundError):
            c.get_image("https://example.com/never.qcow2")


class TestStageLocalIso:
    """Copying arbitrary local ISOs into the cache under a stable,
    content-hashed name."""

    def test_copies_into_cache_when_outside(
        self,
        tmp_path: Path,
        tmp_cache_root: Path,
    ) -> None:
        src = tmp_path / "outside" / "win10.iso"
        src.parent.mkdir()
        src.write_bytes(b"ISO_CONTENTS")

        c = CacheManager(root=tmp_cache_root)
        dest = c.stage_local_iso(src)

        assert dest.exists()
        assert dest.parent == c.images_dir
        assert dest.name.startswith("iso-")
        assert dest.suffix == ".iso"
        assert dest.read_bytes() == b"ISO_CONTENTS"
        # World-readable so the qemu daemon can attach it as a CD-ROM.
        assert dest.stat().st_mode & 0o044

    def test_second_call_is_noop(
        self,
        tmp_path: Path,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        src = tmp_path / "win10.iso"
        src.write_bytes(b"ISO_CONTENTS")
        c = CacheManager(root=tmp_cache_root)

        first = c.stage_local_iso(src)

        # Second call must not re-copy.
        import testrange.cache as cache_mod
        def _explode(*_a, **_k):
            raise AssertionError("should not re-copy on cache hit")
        monkeypatch.setattr(cache_mod, "_copy_file", _explode)

        second = c.stage_local_iso(src)
        assert first == second

    def test_returns_path_unchanged_inside_cache_root(
        self,
        tmp_cache_root: Path,
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        # Place a file directly under the cache root.
        src = c.images_dir / "already-here.iso"
        src.write_bytes(b"noop")
        dest = c.stage_local_iso(src)
        assert dest == src  # no copy

    def test_missing_raises(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        with pytest.raises(ImageNotFoundError):
            c.stage_local_iso(Path("/nonexistent/win10.iso"))


class TestGetVirtioWinIso:
    """Downloads virtio-win.iso into the cache with a stable filename so
    domain XML can reference a known path."""

    def test_downloads_once_and_reuses(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = CacheManager(root=tmp_cache_root)

        called = []
        def _fake_download(url: str, dest):
            called.append(url)
            dest.write_bytes(b"virtio-win-iso-contents")

        monkeypatch.setattr(c, "_download", _fake_download)

        first = c.get_virtio_win_iso()
        assert first == tmp_cache_root / "images" / "virtio-win.iso"
        assert first.read_bytes() == b"virtio-win-iso-contents"
        assert len(called) == 1

        # Second call must be a cache hit — no additional download.
        def _explode(*_a, **_k):
            raise AssertionError("should not download on cache hit")
        monkeypatch.setattr(c, "_download", _explode)

        second = c.get_virtio_win_iso()
        assert second == first

    def test_download_failure_raises_image_not_found(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        c = CacheManager(root=tmp_cache_root)

        def _explode(*_a, **_k):
            raise requests.RequestException("no network")

        monkeypatch.setattr(c, "_download", _explode)
        with pytest.raises(ImageNotFoundError):
            c.get_virtio_win_iso()
