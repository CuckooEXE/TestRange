"""Storage + volume I/O for the libvirt backend (BACKEND-1.A).

Driven against a faithful in-memory fake of the libvirt storage API (pools,
volumes, and the upload/download stream pump) — no daemon, and ``libvirt`` is
never imported. The fake mimics the exact calls the live API exposes (locked by
a live round-trip on the dev host): ``storagePoolDefineXML`` → ``build`` →
``create``; ``pool.createXML``; ``vol.upload``/``download`` driven by
``stream.sendAll``/``recvAll``; ``vol.resize``/``delete``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from testrange.drivers.base import VolumeRef
from testrange.drivers.libvirt import _storage
from testrange.exceptions import DriverError

_GiB = 1024**3


class FakeStream:
    def __init__(self) -> None:
        self.mode: str | None = None
        self.vol: FakeVol | None = None
        self.finished = False
        self.aborted = False

    def sendAll(self, handler: Any, opaque: Any) -> None:
        assert self.mode == "up" and self.vol is not None
        chunks = []
        while True:
            got = handler(self, 1 << 16, opaque)
            if not got:
                break
            chunks.append(got)
        self.vol.data = b"".join(chunks)

    def recvAll(self, handler: Any, opaque: Any) -> None:
        assert self.mode == "down" and self.vol is not None
        data = self.vol.data
        if not data:
            return
        for i in range(0, len(data), 1 << 16):
            handler(self, data[i : i + (1 << 16)], opaque)

    def finish(self) -> None:
        self.finished = True

    def abort(self) -> None:
        self.aborted = True


class FakeVol:
    def __init__(self, name: str, capacity: int, fmt: str) -> None:
        self.name = name
        self.capacity = capacity
        self.fmt = fmt
        self.data = b""
        self.deleted = False

    def upload(self, stream: FakeStream, offset: int, length: int, flags: int) -> None:
        stream.mode, stream.vol = "up", self

    def download(self, stream: FakeStream, offset: int, length: int, flags: int) -> None:
        stream.mode, stream.vol = "down", self

    def resize(self, capacity: int, flags: int) -> None:
        if capacity < self.capacity:
            raise AssertionError("libvirt would reject a shrink")
        self.capacity = capacity

    def delete(self, flags: int) -> None:
        self.deleted = True

    def info(self) -> list[int]:
        return [0, self.capacity, len(self.data)]


class FakePool:
    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        self.vols: dict[str, FakeVol] = {}
        self.active = False
        self.built = False
        self.undefined = False
        self.refreshed = 0
        self.ops: list[str] = []

    def build(self, flags: int) -> None:
        self.built = True
        self.ops.append("build")

    def create(self, flags: int) -> None:
        self.active = True
        self.ops.append("create")

    def isActive(self) -> bool:
        return self.active

    def destroy(self) -> None:
        self.active = False
        self.ops.append("destroy")

    def delete(self, flags: int) -> None:
        self.ops.append("delete")

    def undefine(self) -> None:
        self.undefined = True
        self.ops.append("undefine")

    def refresh(self, flags: int) -> None:
        self.refreshed += 1

    def listAllVolumes(self, flags: int) -> list[FakeVol]:
        return list(self.vols.values())

    def createXML(self, xml: str, flags: int) -> FakeVol:
        root = ET.fromstring(xml)  # noqa: S314 (trusted: driver-generated volume XML)
        name = root.findtext("name") or ""
        capacity = int(root.findtext("capacity") or "0")
        fmt_el = root.find("target/format")
        fmt = fmt_el.get("type", "") if fmt_el is not None else ""
        vol = FakeVol(name, capacity, fmt)
        self.vols[name] = vol
        return vol

    def storageVolLookupByName(self, name: str) -> FakeVol:
        return self.vols[name]  # KeyError surfaces via the client's lookup wrapper


class FakeConn:
    def __init__(self) -> None:
        self.pools: dict[str, FakePool] = {}

    def storagePoolDefineXML(self, xml: str, flags: int) -> FakePool:
        root = ET.fromstring(xml)  # noqa: S314 (trusted: driver-generated pool XML)
        name = root.findtext("name") or ""
        path = root.findtext("target/path") or ""
        pool = FakePool(name, path)
        self.pools[name] = pool
        return pool

    def newStream(self, flags: int) -> FakeStream:
        return FakeStream()


class FakeClient:
    """Duck-typed :class:`LibvirtClient`: ``raw`` + the two lookup helpers."""

    def __init__(self) -> None:
        self.conn = FakeConn()

    @property
    def raw(self) -> FakeConn:
        return self.conn

    def lookup_pool(self, name: str) -> FakePool | None:
        return self.conn.pools.get(name)

    def lookup_volume(self, pool_name: str, vol_name: str) -> FakeVol | None:
        pool = self.conn.pools.get(pool_name)
        if pool is None:
            return None
        return pool.vols.get(vol_name)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def _pool(client: Any, name: str = "tr-pool-abc12345-p1") -> str:
    from testrange.devices.pool.base import StoragePool

    return _storage.create_pool(client, StoragePool("p1", 8), name)


class TestPools:
    def test_create_defines_builds_creates(self, client: Any) -> None:
        ret = _pool(client, "tr-pool-run00000-p1")
        assert ret == "pool:tr-pool-run00000-p1"
        pool = client.conn.pools["tr-pool-run00000-p1"]
        assert pool.path == "/var/lib/libvirt/images/tr-pool-run00000-p1"
        assert pool.ops == ["build", "create"] and pool.active

    def test_destroy_order_and_undefine(self, client: Any) -> None:
        _pool(client, "tr-pool-run00000-p1")
        _storage.destroy_pool(client, "tr-pool-run00000-p1")
        pool = client.conn.pools["tr-pool-run00000-p1"]
        assert pool.ops == ["build", "create", "destroy", "delete", "undefine"]
        assert pool.undefined

    def test_destroy_absent_is_noop(self, client: Any) -> None:
        _storage.destroy_pool(client, "tr-pool-nope-p1")  # no raise

    def test_destroy_sweeps_leftover_volumes(self, client: Any) -> None:
        _pool(client, "tr-pool-run00000-p1")
        ref = VolumeRef("tr-pool-run00000-p1/leftover.qcow2")
        _storage.create_blank_volume(client, ref, 1)
        vol = client.conn.pools["tr-pool-run00000-p1"].vols["leftover.qcow2"]
        _storage.destroy_pool(client, "tr-pool-run00000-p1")
        assert vol.deleted, "leftover volume not swept before pool delete"


class TestVolumeCreation:
    def test_blank_volume_is_sized_qcow2(self, client: Any) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/data0.qcow2")
        assert _storage.create_blank_volume(client, ref, 4) == ref
        vol = client.conn.pools["tr-pool-abc12345-p1"].vols["data0.qcow2"]
        assert vol.fmt == "qcow2" and vol.capacity == 4 * _GiB

    def test_blank_volume_replaces_existing(self, client: Any) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/data0.qcow2")
        _storage.create_blank_volume(client, ref, 4)
        first = client.conn.pools["tr-pool-abc12345-p1"].vols["data0.qcow2"]
        _storage.create_blank_volume(client, ref, 8)
        assert first.deleted
        assert client.conn.pools["tr-pool-abc12345-p1"].vols["data0.qcow2"].capacity == 8 * _GiB

    def test_write_to_pool_is_raw_for_iso_and_roundtrips(self, client: Any) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/seed.iso")
        _storage.write_to_pool(client, ref, b"ISO-BYTES")
        vol = client.conn.pools["tr-pool-abc12345-p1"].vols["seed.iso"]
        assert vol.fmt == "raw" and vol.data == b"ISO-BYTES"

    def test_create_into_missing_pool_raises(self, client: Any) -> None:
        with pytest.raises(DriverError, match=r"pool .* does not exist"):
            _storage.create_blank_volume(client, VolumeRef("tr-pool-missing-p1/x.qcow2"), 4)


class TestStreamIO:
    def test_upload_streams_file_and_refreshes(self, client: Any, tmp_path: Path) -> None:
        _pool(client)
        src = tmp_path / "base.qcow2"
        payload = b"QCOW2-CONTENT" * 1000
        src.write_bytes(payload)
        ref = VolumeRef("tr-pool-abc12345-p1/web.qcow2")
        _storage.upload_to_pool(client, ref, src)
        pool = client.conn.pools["tr-pool-abc12345-p1"]
        assert pool.vols["web.qcow2"].data == payload
        assert pool.refreshed == 1

    def test_upload_is_idempotent(self, client: Any, tmp_path: Path) -> None:
        _pool(client)
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"FIRST")
        ref = VolumeRef("tr-pool-abc12345-p1/web.qcow2")
        _storage.upload_to_pool(client, ref, src)
        first = client.conn.pools["tr-pool-abc12345-p1"].vols["web.qcow2"]
        src.write_bytes(b"SECOND-DIFFERENT")
        _storage.upload_to_pool(client, ref, src)
        # Same object, untouched bytes — no re-upload.
        assert client.conn.pools["tr-pool-abc12345-p1"].vols["web.qcow2"] is first
        assert first.data == b"FIRST"

    def test_download_writes_volume_bytes(self, client: Any, tmp_path: Path) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/web.qcow2")
        src = tmp_path / "in.qcow2"
        src.write_bytes(b"DISK" * 5000)
        _storage.upload_to_pool(client, ref, src)
        dest = tmp_path / "out.qcow2"
        assert _storage.download_from_pool(client, ref, dest) == dest
        assert dest.read_bytes() == b"DISK" * 5000

    def test_download_missing_raises(self, client: Any, tmp_path: Path) -> None:
        _pool(client)
        with pytest.raises(DriverError, match="no volume"):
            _storage.download_from_pool(
                client, VolumeRef("tr-pool-abc12345-p1/gone.qcow2"), tmp_path / "x"
            )


class TestResizeDelete:
    def test_resize_grows_capacity(self, client: Any) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/web.qcow2")
        _storage.create_blank_volume(client, ref, 4)
        _storage.resize_volume(client, ref, 16)
        assert client.conn.pools["tr-pool-abc12345-p1"].vols["web.qcow2"].capacity == 16 * _GiB

    def test_resize_missing_raises(self, client: Any) -> None:
        _pool(client)
        with pytest.raises(DriverError, match="no volume"):
            _storage.resize_volume(client, VolumeRef("tr-pool-abc12345-p1/gone.qcow2"), 16)

    def test_delete_removes_and_tolerates_absence(self, client: Any) -> None:
        _pool(client)
        ref = VolumeRef("tr-pool-abc12345-p1/web.qcow2")
        _storage.create_blank_volume(client, ref, 4)
        vol = client.conn.pools["tr-pool-abc12345-p1"].vols["web.qcow2"]
        _storage.delete_volume(client, ref)
        assert vol.deleted
        _storage.delete_volume(client, ref)  # second call: no raise


class TestRefParsing:
    @pytest.mark.parametrize("bad", ["nopool", "/leadingslash", "trailing/", ""])
    def test_malformed_ref_raises(self, client: Any, bad: str) -> None:
        with pytest.raises(DriverError, match="malformed"):
            _storage.delete_volume(client, VolumeRef(bad))
