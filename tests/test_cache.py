"""Unit tests for :mod:`testrange.cache`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from testrange.cache import CacheManager, _sha256_file, vm_config_hash
from testrange.exceptions import ImageNotFoundError
from testrange.backends.libvirt.storage import LocalStorageBackend


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

    def test_cache_hit_per_vm_dir(self, tmp_cache_root: Path) -> None:
        """A populated <vms_dir>/<hash>/ directory with disk + manifest
        is a cache hit; the returned ref points at the disk file
        inside the per-VM directory."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        vm = c.vms_dir / "abc123"
        vm.mkdir()
        disk = vm / "disk.qcow2"
        disk.write_bytes(b"fake")
        (vm / "manifest.json").write_text("{}")
        # For LocalStorageBackend, the ref is just the absolute path.
        assert c.get_vm("abc123", b) == str(disk)

    def test_disk_without_manifest_is_cache_miss(
        self, tmp_cache_root: Path
    ) -> None:
        """A disk with no sibling manifest is the footprint of a
        crashed ``store_vm`` — treat it as a miss so the rebuild
        overwrites it instead of booting from a truncated image."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        vm = c.vms_dir / "partial"
        vm.mkdir()
        (vm / "disk.qcow2").write_bytes(b"truncated")
        assert c.get_vm("partial", b) is None

    def test_manifest_without_disk_is_cache_miss(
        self, tmp_cache_root: Path
    ) -> None:
        """Symmetric: an orphan manifest (e.g. someone deleted the
        disk) should also miss."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        vm = c.vms_dir / "ghost"
        vm.mkdir()
        (vm / "manifest.json").write_text("{}")
        assert c.get_vm("ghost", b) is None

    def test_vm_paths(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        assert (
            c.vm_dir("abc", b)
            == str(tmp_cache_root / "vms" / "abc")
        )
        assert (
            c.vm_disk_ref("abc", b)
            == str(tmp_cache_root / "vms" / "abc" / "disk.qcow2")
        )
        assert (
            c.vm_manifest_ref("abc", b)
            == str(tmp_cache_root / "vms" / "abc" / "manifest.json")
        )
        # Backends drop additional resources (extra disks, hypervisor-
        # specific config blobs, …) into the same per-VM directory by
        # passing an arbitrary name to vm_resource_ref.
        assert (
            c.vm_resource_ref("abc", "disk-1.qcow2", b)
            == str(tmp_cache_root / "vms" / "abc" / "disk-1.qcow2")
        )


class TestStoreVm:
    def test_writes_per_vm_dir(
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

        # Disk + manifest land inside the per-VM directory.
        assert dest == tmp_cache_root / "vms" / "abc123" / "disk.qcow2"
        assert (tmp_cache_root / "vms" / "abc123" / "manifest.json").exists()
        # No flat-layout files left behind.
        assert not (tmp_cache_root / "vms" / "abc123.qcow2").exists()
        assert not (tmp_cache_root / "vms" / "abc123.json").exists()

    def test_manifest_contents(
        self,
        tmp_cache_root: Path,
        fake_qemu_img: list[list[str]],
    ) -> None:
        """The manifest.json must be inspectable and record what built the image."""
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
            (tmp_cache_root / "vms" / "abc123" / "manifest.json").read_text()
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

    def test_compress_writes_to_partial_then_renames(
        self,
        tmp_cache_root: Path,
        fake_qemu_img: list[list[str]],
    ) -> None:
        """qemu-img must target ``disk.qcow2.partial`` — not the
        final path — so a crash can't leave a plausible cache file."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "work.qcow2"
        src.write_bytes(b"x")
        c.store_vm("abc", str(src), {"name": "v"}, b)

        convert_calls = [cmd for cmd in fake_qemu_img if cmd[:2] == ["qemu-img", "convert"]]
        assert convert_calls, "expected a qemu-img convert call"
        assert convert_calls[-1][-1].endswith(".qcow2.partial"), (
            f"compress must write to .partial, got argv={convert_calls[-1]!r}"
        )
        # After a successful store_vm the partial must be gone.
        vm_dir = tmp_cache_root / "vms" / "abc"
        assert not (vm_dir / "disk.qcow2.partial").exists()
        assert (vm_dir / "disk.qcow2").exists()

    def test_compress_failure_leaves_no_partial_behind(
        self,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If compress raises mid-write, the .partial stub must not
        linger — and critically, no final-named qcow2 is ever created,
        so the next run re-enters ``store_vm`` instead of getting a
        false cache hit."""
        c = CacheManager(root=tmp_cache_root)
        b = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "work.qcow2"
        src.write_bytes(b"x")

        from testrange.backends.libvirt import _qcow2 as _qcow2_mod

        def _boom(self: object, src_ref: str, dest_ref: str) -> None:
            # Simulate a compress that writes a partial file and then
            # dies — matches the OOM-kill we actually observed.
            Path(dest_ref).write_bytes(b"partial")
            raise RuntimeError("simulated OOM")

        monkeypatch.setattr(_qcow2_mod.Qcow2DiskFormat, "compress", _boom)

        with pytest.raises(RuntimeError, match="simulated OOM"):
            c.store_vm("abc", str(src), {"name": "v"}, b)

        vm_dir = tmp_cache_root / "vms" / "abc"
        assert not (vm_dir / "disk.qcow2.partial").exists()
        assert not (vm_dir / "disk.qcow2").exists()
        assert not (vm_dir / "manifest.json").exists()


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


class TestStageSourceAtomic:
    """``stage_source`` uploads to a ``.part`` sibling and atomic-
    renames to the final content-addressed path.  Without this, an
    interrupted upload leaves a partial file at the final ref that
    passes ``exists()`` next run and gets returned as a "cache hit"
    — silent corruption that propagates into VM builds."""

    def _stub_remote_backend(self, tmp_cache_root: Path) -> object:
        """Build a backend that pretends to be remote (so the local
        fast-path doesn't kick in) but records uploads in-memory."""
        from unittest.mock import MagicMock

        transport = MagicMock()
        transport.images_dir.return_value = "/remote/images"
        transport._join.side_effect = lambda *parts: "/".join(parts)
        # Track what's been written.
        written: dict[str, bytes] = {}

        def _exists(ref: str) -> bool:
            return ref in written

        def _upload(local_path: Path, ref: str) -> None:
            written[ref] = local_path.read_bytes()

        def _remove(ref: str) -> None:
            written.pop(ref, None)

        def _rename(src: str, dst: str) -> None:
            written[dst] = written.pop(src)

        transport.exists.side_effect = _exists
        transport.upload.side_effect = _upload
        transport.remove.side_effect = _remove
        transport.rename.side_effect = _rename

        backend = MagicMock()
        backend.transport = transport
        # Return a sentinel as an MagicMock attribute so the
        # ``_transport_is_local`` check returns False for our stub.
        return backend, transport, written

    def test_uploads_to_part_then_renames(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange import cache as cache_mod

        # Force the "not local" branch so we exercise the upload path.
        monkeypatch.setattr(
            cache_mod, "_transport_is_local", lambda _t: False,
        )

        c = CacheManager(root=tmp_cache_root)
        src = tmp_cache_root / "src.iso"
        src.write_bytes(b"image bytes")

        backend, transport, written = self._stub_remote_backend(tmp_cache_root)
        ref = c.stage_source(src, backend)

        # Final ref doesn't carry ``.part`` — we renamed.
        assert not ref.endswith(".part")
        # Only the final ref is in the backend; the ``.part`` was
        # renamed away (not left lingering).
        assert ref in written
        assert f"{ref}.part" not in written

    def test_failed_upload_cleans_up_part(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: an interrupted upload (Ctrl+C, OOM, network
        blip) used to leave a partial file at the final content-
        addressed path, where the next run's ``exists()`` check
        would treat it as a cache hit.  Now uploads target a
        ``.part`` sibling and the partial gets cleaned on failure
        — the final path stays empty so the next run re-uploads."""
        from testrange import cache as cache_mod

        monkeypatch.setattr(
            cache_mod, "_transport_is_local", lambda _t: False,
        )

        c = CacheManager(root=tmp_cache_root)
        src = tmp_cache_root / "src.iso"
        src.write_bytes(b"image bytes")

        backend, transport, written = self._stub_remote_backend(tmp_cache_root)

        # Make ``upload`` half-write then raise — mimics a network
        # interruption mid-upload.
        def _bad_upload(local_path: Path, ref: str) -> None:
            written[ref] = b"half"  # partial
            raise IOError("connection reset")

        transport.upload.side_effect = _bad_upload

        with pytest.raises(IOError, match="connection reset"):
            c.stage_source(src, backend)

        # Neither the .part nor the final ref is left behind.
        assert all(not k.endswith(".part") for k in written)
        # And the final content-addressed ref is unwritten — next
        # run will retry rather than serving the partial as a hit.
        digest_refs = [
            k for k in written if k.startswith("/remote/images/")
        ]
        assert not digest_refs


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


# ----------------------------------------------------------------------
# UEFI NVRAM sidecar — the install-phase NVRAM file holds boot
# entries that PVE (and any other distro that doesn't write
# ``/EFI/BOOT/BOOTX64.EFI``) needs preserved across the install/run
# domain rebuild.  Without these tests a future cache refactor could
# silently break ProxMox installs.
# ----------------------------------------------------------------------


class TestVmNvramRef:
    def test_path_format(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        backend = _local_backend(tmp_cache_root)
        ref = c.vm_nvram_ref("abc123def", backend)
        assert ref.endswith("/vms/abc123def.nvram.fd")


class TestStoreVmNvram:
    def test_round_trip(self, tmp_cache_root: Path) -> None:
        """``store_vm_nvram`` must round-trip the install-phase NVRAM
        bytes verbatim — the file is opaque OVMF state, any byte
        change risks corrupting the boot variables."""
        c = CacheManager(root=tmp_cache_root)
        backend = _local_backend(tmp_cache_root)

        # Install-phase NVRAM somewhere outside the cache (this is
        # what a RunDir's nvram_path looks like in production).
        src = tmp_cache_root / "fake-run" / "proxmox_VARS.fd"
        src.parent.mkdir()
        body = bytes(range(256)) * 2  # 512 distinct-ish bytes
        src.write_bytes(body)

        dest_ref = c.store_vm_nvram("h1234567890", str(src), backend)
        assert Path(dest_ref).read_bytes() == body
        assert Path(dest_ref).name == "h1234567890.nvram.fd"

    def test_atomic_partial_rename(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mid-write crashes must not leave a half-written .nvram.fd
        the next run mistakes for a valid sidecar — same atomic
        ``.partial`` discipline as ``store_vm``.
        """
        c = CacheManager(root=tmp_cache_root)
        backend = _local_backend(tmp_cache_root)

        src = tmp_cache_root / "src.fd"
        src.write_bytes(b"x" * 100)

        # Simulate a write_bytes that raises after writing the
        # partial; the helper's except branch must remove the partial.
        original_write = backend.transport.write_bytes
        partial_seen: list[Path] = []

        def _explode(ref: str, data: bytes, mode: int = 0o644) -> None:
            original_write(ref, data, mode)
            if ref.endswith(".partial"):
                partial_seen.append(Path(ref))
                raise RuntimeError("simulated crash")

        monkeypatch.setattr(backend.transport, "write_bytes", _explode)

        with pytest.raises(RuntimeError, match="simulated"):
            c.store_vm_nvram("hh", str(src), backend)

        assert partial_seen, "partial path must have been written before crash"
        assert not partial_seen[0].exists(), (
            "partial must be removed after the crash so the next "
            "store_vm_nvram doesn't inherit it"
        )
        # Final dest must NOT exist either — half-write isn't a cache hit.
        assert not (tmp_cache_root / "vms" / "hh.nvram.fd").exists()


class TestGetVmNvram:
    def test_miss_returns_none(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        backend = _local_backend(tmp_cache_root)
        assert c.get_vm_nvram("never-stored", backend) is None

    def test_hit_returns_ref(self, tmp_cache_root: Path) -> None:
        c = CacheManager(root=tmp_cache_root)
        backend = _local_backend(tmp_cache_root)
        src = tmp_cache_root / "src.fd"
        src.write_bytes(b"contents")
        c.store_vm_nvram("h", str(src), backend)
        ref = c.get_vm_nvram("h", backend)
        assert ref is not None
        assert Path(ref).read_bytes() == b"contents"


# ----------------------------------------------------------------------
# Proxmox prepared-ISO cache — the expensive 1-time prep result that
# subsequent VMs reuse.
# ----------------------------------------------------------------------


class TestGetProxmoxPreparedIso:
    def test_first_call_invokes_prep(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        vanilla = tmp_cache_root / "vanilla.iso"
        vanilla.write_bytes(b"vanilla iso contents")

        prep_calls: list[tuple[Path, Path]] = []

        def _fake_prep(src, dst, *, partition_label="PROXMOX-AIS"):
            prep_calls.append((Path(src), Path(dst)))
            Path(dst).write_bytes(b"prepared iso contents")

        # Patch the symbol the cache method imports lazily.
        import testrange.vms.builders._proxmox_prepare as pp
        monkeypatch.setattr(pp, "prepare_iso_bytes", _fake_prep)

        prepared = c.get_proxmox_prepared_iso(vanilla)
        assert prepared.exists()
        assert prepared.read_bytes() == b"prepared iso contents"
        assert len(prep_calls) == 1
        # Cache filename embeds a sha256 prefix of the vanilla bytes.
        assert prepared.name.startswith("proxmox-prepared-")
        assert prepared.name.endswith(".iso")

    def test_cache_hit_skips_prep(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c = CacheManager(root=tmp_cache_root)
        vanilla = tmp_cache_root / "vanilla.iso"
        vanilla.write_bytes(b"v")

        import testrange.vms.builders._proxmox_prepare as pp

        first_calls: list[Any] = []
        monkeypatch.setattr(
            pp, "prepare_iso_bytes",
            lambda s, d, **_: (first_calls.append((s, d)),
                              Path(d).write_bytes(b"prepared"))[1],
        )
        first = c.get_proxmox_prepared_iso(vanilla)

        def _explode(*_a, **_k):
            raise AssertionError("must not reprep on cache hit")
        monkeypatch.setattr(pp, "prepare_iso_bytes", _explode)

        second = c.get_proxmox_prepared_iso(vanilla)
        assert second == first

    def test_partial_cleaned_on_prep_failure(
        self, tmp_cache_root: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``prepare_iso_bytes`` raises mid-write, the
        ``.iso.part`` stub must be removed — otherwise concurrent
        callers could race the partial and confuse cache reads."""
        c = CacheManager(root=tmp_cache_root)
        vanilla = tmp_cache_root / "vanilla.iso"
        vanilla.write_bytes(b"v")

        import testrange.vms.builders._proxmox_prepare as pp

        def _explode(_src, dst, **_kw):
            Path(dst).write_bytes(b"half-written")
            raise RuntimeError("prep crash")
        monkeypatch.setattr(pp, "prepare_iso_bytes", _explode)

        with pytest.raises(RuntimeError, match="prep crash"):
            c.get_proxmox_prepared_iso(vanilla)

        leftover = list((tmp_cache_root / "images").glob("proxmox-prepared-*.iso.part"))
        assert leftover == [], (
            f"expected no .part files after crash; found {leftover!r}"
        )
