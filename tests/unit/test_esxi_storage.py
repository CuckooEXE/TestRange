"""ESXI-3: datastore pool + volume I/O orchestration via fakes.

The qcow2<->vmdk conversion itself is covered by test_diskconvert; here the
conversion is stubbed so the tests exercise only the driver's pool/volume
orchestration (idempotency, ref handling, datastore guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.drivers.base import VolumeRef
from testrange.drivers.esxi import _storage
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver
from testrange.exceptions import DriverError
from tests.esxi_fakes import FakeEsxiClient


def _driver(client: FakeEsxiClient) -> ESXiDriver:
    return ESXiDriver(EsxiConn(host="h", datastore="datastore1"), client=client)  # type: ignore[arg-type]


def _ref(client: FakeEsxiClient, name: str) -> VolumeRef:
    return ESXiDriver(
        EsxiConn(host="h"),
        client=client,  # type: ignore[arg-type]
    ).compose_volume_ref("pool1", name)


def test_create_pool_makes_directory() -> None:
    client = FakeEsxiClient()
    _driver(client).create_pool(object(), "pool1")  # type: ignore[arg-type]
    assert "[datastore1] pool1" in client.dirs


def test_create_blank_volume_then_resize() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "web.qcow2")
    d.create_blank_volume(ref, 8)
    assert client.folder_exists("pool1/web.vmdk")
    d.resize_volume(ref, 16)
    assert client.files["pool1/web.vmdk"] == b"\x00" * 32  # extended marker


def test_write_to_pool_puts_iso() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "seed.iso")
    d.write_to_pool(ref, b"isodata")
    assert client.files["pool1/seed.iso"] == b"isodata"


def test_upload_idempotent_skip_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "os.qcow2")
    client.files["pool1/os.vmdk"] = b"already-here"
    called = False

    def _no_convert(*a: object, **k: object) -> Path:
        nonlocal called
        called = True
        return Path("x")

    monkeypatch.setattr("testrange.drivers._diskconvert.qcow2_to_vmdk", _no_convert)
    d.upload_to_pool(ref, Path("/nonexistent.qcow2"))
    assert not called, "idempotent upload must not re-convert/re-ingest"
    assert client.files["pool1/os.vmdk"] == b"already-here"


def test_upload_ingests_and_cleans_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "os.qcow2")
    src = tmp_path / "src.qcow2"
    src.write_bytes(b"qcow2-source")

    def _fake_convert(source: Path, dst: Path, *, subformat: str = "") -> Path:
        Path(dst).write_bytes(b"vmdk-stage")
        return dst

    monkeypatch.setattr("testrange.drivers._diskconvert.qcow2_to_vmdk", _fake_convert)
    d.upload_to_pool(ref, src)
    # CopyVirtualDisk inflated the staging into the dest disk...
    assert client.folder_exists("pool1/os.vmdk")
    # ...and the staging vmdk was cleaned up.
    assert not any("stage" in k for k in client.files), "staging vmdk not cleaned up"


def test_upload_stages_on_cache_filesystem(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # ESXI-31: the multi-GiB qcow2->vmdk staging must land on the cache filesystem
    # (next to the source), not the default tempdir — /tmp is a small tmpfs on most
    # hosts and three concurrent 8 GiB flat-vmdk stages overflow it ([Errno 28]).
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "os.qcow2")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    src = cache_dir / "src.qcow2"
    src.write_bytes(b"qcow2-source")

    put_sources: list[Path] = []
    real_put = client.folder_put

    def _spy_put(source_path: Path, ds_path: str) -> None:
        put_sources.append(Path(source_path))
        real_put(source_path, ds_path)

    monkeypatch.setattr(client, "folder_put", _spy_put)
    monkeypatch.setattr(
        "testrange.drivers._diskconvert.qcow2_to_vmdk",
        lambda source, dst, *, subformat="": Path(dst).write_bytes(b"vmdk-stage"),
    )

    d.upload_to_pool(ref, src)
    assert put_sources, "upload never staged a local file for /folder PUT"
    for staged in put_sources:
        assert cache_dir.resolve() in staged.resolve().parents, (
            f"upload staged {staged} outside the cache filesystem {cache_dir}"
        )


def test_download_stages_on_cache_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ESXI-31: the multi-GiB descriptor+flat-vmdk export must land next to the cache
    # destination, not the default tempdir (tmpfs /tmp), or it overflows under --jobs.
    client = FakeEsxiClient()
    d = _driver(client)
    ref = _ref(client, "os.qcow2")
    client.files["pool1/os.vmdk"] = b"descriptor"
    client.files["pool1/os-flat.vmdk"] = b"flat-extent"

    get_dests: list[Path] = []
    real_get = client.folder_get

    def _spy_get(ds_path: str, dest_path: Path) -> None:
        get_dests.append(Path(dest_path))
        real_get(ds_path, dest_path)

    monkeypatch.setattr(client, "folder_get", _spy_get)
    monkeypatch.setattr(
        "testrange.drivers._diskconvert.vmdk_to_qcow2",
        lambda desc, dst, *a, **k: Path(dst).write_bytes(b"qcow2-out"),
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    dest = cache_dir / "os.qcow2"
    d.download_from_pool(ref, dest)
    assert get_dests, "download never fetched a local staging file"
    for staged in get_dests:
        assert cache_dir.resolve() in staged.resolve().parents, (
            f"download staged {staged} outside the cache filesystem {cache_dir}"
        )


def test_delete_volume_disk_and_iso_tolerant() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    disk = _ref(client, "web.qcow2")
    d.create_blank_volume(disk, 8)
    d.delete_volume(disk)
    assert not client.folder_exists("pool1/web.vmdk")
    # tolerant of absence
    d.delete_volume(disk)
    iso = _ref(client, "seed.iso")
    d.write_to_pool(iso, b"x")
    d.delete_volume(iso)
    assert not client.folder_exists("pool1/seed.iso")


def test_wrong_datastore_ref_rejected() -> None:
    client = FakeEsxiClient()
    with pytest.raises(DriverError, match="not the connected"):
        _storage.create_blank_volume(client, VolumeRef("[other-ds] pool1/x.vmdk"), 8)  # type: ignore[arg-type]


def test_destroy_pool_tolerant() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    d.create_pool(object(), "pool1")  # type: ignore[arg-type]
    d.destroy_pool("pool1")
    assert "[datastore1] pool1" not in client.dirs
    d.destroy_pool("pool1")  # idempotent
