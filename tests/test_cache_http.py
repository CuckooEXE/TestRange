"""Tests for the HTTP-cache integration in :mod:`testrange.cache`.

The remote cache is a thin adapter over ``requests.Session``; we
verify the *integration* (CacheManager + remote) here using a fake
HttpCache that records calls and serves canned responses.  The HTTP
client itself is exercised against the real ``cache/`` docker stack
in a live-fixture test (out of scope for this offline suite).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.cache import CacheManager
from testrange.cache_http import HttpCache
from testrange.storage import LocalStorageBackend


class _FakeRemote:
    """In-memory stand-in for :class:`HttpCache` for offline tests.

    Records every method call so tests can assert on the integration
    behaviour without spinning up a real HTTP server.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []  # (verb, key)

    # ------------- canned-response helpers used by tests --------------

    def seed(self, key: str, data: bytes) -> None:
        self.store[key] = data

    # ------------- HttpCache surface ----------------------------------

    def exists(self, path: str) -> bool:
        self.calls.append(("exists", path))
        return path in self.store

    def get(self, path: str, dest: Path) -> bool:
        self.calls.append(("get", path))
        if path not in self.store:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.store[path])
        return True

    def put(self, path: str, src: Path) -> bool:
        self.calls.append(("put", path))
        self.store[path] = src.read_bytes()
        return True

    def delete(self, path: str) -> bool:
        self.calls.append(("delete", path))
        return self.store.pop(path, None) is not None


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    """An empty CacheManager root for the test."""
    return tmp_path / "cache"


# ---------------------------------------------------------------------
# get_image: remote-fill and remote-publish
# ---------------------------------------------------------------------


def test_get_image_fills_from_remote_on_local_miss(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the remote has the image, no upstream download happens."""
    remote = _FakeRemote()
    url = "https://example.com/debian-12.qcow2"
    # Pre-seed the remote as if a previous run had pushed it.
    import hashlib
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
    remote.seed(f"images/{url_hash}.qcow2", b"qcow2-bytes")

    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]

    # If the upstream download is reached we want a hard failure, not
    # a silent network call — monkeypatch _download to detonate.
    def _boom(url: str, dest: Path) -> None:
        raise AssertionError(f"unexpected upstream download from {url}")

    monkeypatch.setattr(cm, "_download", _boom)

    path = cm.get_image(url)
    assert path.read_bytes() == b"qcow2-bytes"
    assert ("get", f"images/{url_hash}.qcow2") in remote.calls
    # Synthetic meta sidecar created when the remote didn't have one.
    assert (tmp_cache / "images" / f"{url_hash}.meta.json").exists()


def test_get_image_publishes_to_remote_after_cold_download(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After downloading from upstream, the image is PUT to the remote."""
    remote = _FakeRemote()
    url = "https://example.com/alpine.qcow2"

    def _fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"freshly-downloaded")

    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]
    monkeypatch.setattr(cm, "_download", _fake_download)

    cm.get_image(url)

    import hashlib
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
    assert remote.store[f"images/{url_hash}.qcow2"] == b"freshly-downloaded"
    # Meta sidecar published too.
    assert f"images/{url_hash}.meta.json" in remote.store


def test_get_image_no_remote_means_no_calls(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a remote configured, behaviour matches pre-HTTP-cache code."""
    cm = CacheManager(root=tmp_cache)  # remote defaults to None

    def _fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"x")

    monkeypatch.setattr(cm, "_download", _fake_download)
    cm.get_image("https://example.com/x.qcow2")
    # Nothing to assert about the remote — just confirm no AttributeError
    # path was taken (we got here without crashing on .remote being None).


# ---------------------------------------------------------------------
# get_vm / store_vm: remote-fill and remote-publish
# ---------------------------------------------------------------------


def test_get_vm_fills_from_remote(tmp_cache: Path) -> None:
    """Remote hit populates disk + manifest into the per-VM directory."""
    remote = _FakeRemote()
    config_hash = "abc123" + "0" * 18
    remote.seed(f"libvirt/vms/{config_hash}/disk.qcow2", b"disk-bytes")
    remote.seed(f"libvirt/vms/{config_hash}/manifest.json", b'{"name": "vm"}')

    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]
    cm.backend_name = "libvirt"
    backend = LocalStorageBackend(cache_root=tmp_cache)

    ref = cm.get_vm(config_hash, backend)
    assert ref is not None
    assert Path(ref).read_bytes() == b"disk-bytes"
    manifest_ref = cm.vm_manifest_ref(config_hash, backend)
    assert Path(manifest_ref).read_bytes() == b'{"name": "vm"}'


def test_get_vm_remote_miss_returns_none(tmp_cache: Path) -> None:
    """No local + no remote = miss, no synthetic state left behind."""
    remote = _FakeRemote()
    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]
    cm.backend_name = "libvirt"
    backend = LocalStorageBackend(cache_root=tmp_cache)

    assert cm.get_vm("missing" + "0" * 17, backend) is None
    # Nothing should have landed on disk either.
    assert list((tmp_cache / "vms").glob("missing*")) == []


def test_get_vm_remote_partial_hit_drops_orphan(tmp_cache: Path) -> None:
    """Disk present remotely but no manifest → discard the partial fill."""
    remote = _FakeRemote()
    config_hash = "partial" + "0" * 17
    remote.seed(f"libvirt/vms/{config_hash}/disk.qcow2", b"disk-bytes")
    # No manifest seeded.

    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]
    cm.backend_name = "libvirt"
    backend = LocalStorageBackend(cache_root=tmp_cache)

    assert cm.get_vm(config_hash, backend) is None
    # Disk must NOT remain in the local cache — otherwise the next
    # get_vm would see a disk-without-manifest and log the
    # "partial write" warning intended for crashed local stores.
    disk_ref = cm.vm_disk_ref(config_hash, backend)
    assert not Path(disk_ref).exists()


def test_store_vm_publishes_to_remote(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful store mirrors the disk + manifest to the remote
    under the configured backend's URL prefix."""
    remote = _FakeRemote()
    cm = CacheManager(root=tmp_cache, remote=remote)  # type: ignore[arg-type]
    cm.backend_name = "libvirt"
    backend = LocalStorageBackend(cache_root=tmp_cache)

    # Stand in for the disk-format compress step: just copy the bytes.
    src = tmp_cache / "src.qcow2"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"installed-disk")

    def _fake_compress(self, src_ref: str, dst_ref: str) -> None:  # type: ignore[no-untyped-def]
        Path(dst_ref).write_bytes(Path(src_ref).read_bytes())

    from testrange.storage.disk import qcow2 as _qcow2_mod
    monkeypatch.setattr(_qcow2_mod.Qcow2DiskFormat, "compress", _fake_compress)

    config_hash = "hash" + "0" * 20
    cm.store_vm(config_hash, str(src), {"name": "vm"}, backend)

    assert (
        remote.store[f"libvirt/vms/{config_hash}/disk.qcow2"]
        == b"installed-disk"
    )
    assert f"libvirt/vms/{config_hash}/manifest.json" in remote.store


def test_remote_keys_include_backend_prefix() -> None:
    """Different backend_name values produce sibling URL keyspaces so
    a single remote can serve multiple backends without collisions.
    backend_name is set by the orchestrator that owns the cache,
    not by user code."""
    libvirt_cm = CacheManager()
    libvirt_cm.backend_name = "libvirt"
    proxmox_cm = CacheManager()
    proxmox_cm.backend_name = "proxmox"
    h = "x" * 24
    assert (
        libvirt_cm._remote_vm_resource_key(h, "disk.qcow2")
        == f"libvirt/vms/{h}/disk.qcow2"
    )
    assert (
        proxmox_cm._remote_vm_resource_key(h, "disk.qcow2")
        == f"proxmox/vms/{h}/disk.qcow2"
    )


def test_store_vm_skips_remote_publish_when_remote_is_none(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a remote, store_vm matches the pre-HTTP-cache behaviour."""
    cm = CacheManager(root=tmp_cache)
    backend = LocalStorageBackend(cache_root=tmp_cache)

    src = tmp_cache / "src.qcow2"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x")

    def _fake_compress(self, src_ref: str, dst_ref: str) -> None:  # type: ignore[no-untyped-def]
        Path(dst_ref).write_bytes(Path(src_ref).read_bytes())

    from testrange.storage.disk import qcow2 as _qcow2_mod
    monkeypatch.setattr(_qcow2_mod.Qcow2DiskFormat, "compress", _fake_compress)

    cm.store_vm("h" + "0" * 23, str(src), {"name": "vm"}, backend)
    # No assertion on remote calls — we just verified no crash on
    # the no-remote codepath.


# ---------------------------------------------------------------------
# HttpCache constructor sanity
# ---------------------------------------------------------------------


def test_http_cache_strips_trailing_slash() -> None:
    c = HttpCache("https://cache.testrange/")
    assert c.base_url == "https://cache.testrange"
    assert c._url("vms/x.qcow2") == "https://cache.testrange/vms/x.qcow2"


def test_http_cache_verify_passed_to_session() -> None:
    c = HttpCache("https://cache.testrange", verify=False)
    assert c.verify is False
    assert c._session.verify is False
