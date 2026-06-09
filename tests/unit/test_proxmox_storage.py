"""PVE-3: pool + volume I/O, incl. the Option-2 ``download_from_pool`` resolution.

A chained fake API (proxmoxer-style) plus SFTP call recording — no proxmoxer, no
real PVE. The focus is the asymmetry: upload/write/delete act on the *staging*
volume the ref names, while download re-resolves the ref to the *live* vm-scoped
disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testrange.drivers.base import VolumeRef
from testrange.drivers.proxmox import _naming, _storage
from testrange.exceptions import DriverError


class _Endpoint:
    def __init__(self, api: _FakeApi, path: str) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "post", "put", "delete"):
            return lambda **kw: self._api._call(name, self._path, kw)
        return _Endpoint(self._api, f"{self._path}/{name}")

    def __call__(self, *args: Any) -> _Endpoint:
        return _Endpoint(self._api, f"{self._path}/{'/'.join(str(a) for a in args)}")


class _FakeApi:
    def __init__(self) -> None:
        self.vms: list[dict[str, Any]] = []
        self.configs: dict[int, dict[str, str]] = {}
        self.content_vols: list[dict[str, str]] = []
        self.deleted: list[str] = []
        self.delete_raises = False

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path.endswith("/qemu") and method == "get":
            return self.vms
        if path.endswith("/config") and method == "get":
            vmid = int(path.split("/qemu/", 1)[1].split("/", 1)[0])
            return self.configs[vmid]
        if path.endswith("/content") and method == "get":
            return self.content_vols
        if "/content/" in path and method == "delete":
            if self.delete_raises:
                raise RuntimeError("simulated 404")
            self.deleted.append(path.split("/content/", 1)[1])
            return None
        raise AssertionError(f"unexpected API call: {method} {path} {kwargs}")


class _FakeClient:
    def __init__(self, node: str = "ns1001849", storage: str = "local") -> None:
        self.api = _FakeApi()
        self.node = node
        self.storage = storage
        self.put: list[tuple[str, bytes | None]] = []  # (remote, bytes if source exists)
        self.got: list[tuple[str, str]] = []  # (remote, dest)
        self.waited: list[str] = []

    def storage_path(self) -> str:
        return "/var/lib/vz"

    def sftp_put(self, source_path: Path, remote_path: str) -> None:
        data = source_path.read_bytes() if source_path.exists() else None
        self.put.append((remote_path, data))

    def sftp_get(self, remote_path: str, dest_path: Path) -> None:
        self.got.append((remote_path, str(dest_path)))

    def wait_task(self, upid: str, *, timeout: float = 600.0) -> None:
        self.waited.append(upid)


def _client() -> Any:
    return _FakeClient()


class TestStaging:
    def test_upload_sftps_import_into_storage_dir(self) -> None:
        # PVE-23: disk images go up via SFTP into <storage>/import/, not the REST
        # upload endpoint (which 501s on large imports).
        c = _client()
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web.qcow2")
        _storage.upload_to_pool(c, ref, Path("/cache/abc123.bin"))
        assert c.put == [("/var/lib/vz/import/tr-pool-x-p1__tr-vm-x-web.qcow2", None)]
        assert c.got == []  # upload is a put, never a get

    def test_upload_skips_when_already_staged(self) -> None:
        # PVE-25: the ABC contracts upload_to_pool as idempotent — a volume already
        # at the ref must not be re-transferred (multi-GB on a retry/resume).
        c = _client()
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web.qcow2")
        c.api.content_vols = [{"volid": str(ref)}]  # already staged
        _storage.upload_to_pool(c, ref, Path("/cache/abc123.bin"))
        assert c.put == []  # short-circuited, no re-upload

    def test_write_sftps_iso_into_template_dir(self) -> None:
        c = _client()
        ref = VolumeRef("local:iso/tr-pool-x-p1__seed.iso")
        _storage.write_to_pool(c, ref, b"ISO-BYTES")
        # Staged bytes land at the iso content path, where dir storage scans them.
        assert c.put == [("/var/lib/vz/template/iso/tr-pool-x-p1__seed.iso", b"ISO-BYTES")]


class TestDownload:
    def _vm_with_disk(self, c: Any, vmid: int, name: str, scsi0: str) -> None:
        c.api.vms = [{"vmid": vmid, "name": name}]
        c.api.configs[vmid] = {"scsi0": scsi0}

    def test_download_reads_live_vm_disk_not_staging(self) -> None:
        c = _client()
        # the build VM exists and its scsi0 is the vm-scoped baked disk
        self._vm_with_disk(c, 107, "tr-vm-x-web", "local:107/vm-107-disk-0.qcow2,size=8G")
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web.qcow2")
        _storage.download_from_pool(c, ref, Path("/tmp/out.qcow2"))
        (remote, dest) = c.got[0]
        assert remote == "/var/lib/vz/images/107/vm-107-disk-0.qcow2"
        assert dest == "/tmp/out.qcow2"

    def test_download_finds_disk_on_a_non_scsi_bus(self) -> None:
        # A ProxmoxHardDrive(bus="virtio") data disk lives at slot 1 on virtio,
        # not scsi; download_from_pool must scan buses for the slot (capture stays
        # bus-agnostic).
        c = _client()
        c.api.vms = [{"vmid": 107, "name": "tr-vm-x-web"}]
        c.api.configs[107] = {
            "scsi0": "local:107/vm-107-disk-0.qcow2",
            "virtio1": "local:107/vm-107-disk-1.qcow2,size=1G",
        }
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web-data0.qcow2")
        _storage.download_from_pool(c, ref, Path("/tmp/out.qcow2"))
        (remote, _dest) = c.got[0]
        assert remote.endswith("vm-107-disk-1.qcow2")

    def test_download_missing_disk_entry_raises(self) -> None:
        c = _client()
        c.api.vms = [{"vmid": 107, "name": "tr-vm-x-web"}]
        c.api.configs[107] = {}  # no disk on any bus
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web.qcow2")
        with pytest.raises(DriverError, match="no disk at slot 0"):
            _storage.download_from_pool(c, ref, Path("/tmp/out.qcow2"))

    def test_download_never_resolves_to_a_cdrom_at_a_colliding_slot(self) -> None:
        # PVE-55: a seed/boot CDROM (ide0/ide2) sharing a slot index with the
        # resolved disk must NOT be matched — that would download the ISO. With
        # only the cdrom present at the slot, resolution fails loud instead.
        c = _client()
        c.api.vms = [{"vmid": 107, "name": "tr-vm-x-web"}]
        c.api.configs[107] = {
            "scsi0": "local:107/vm-107-disk-0.qcow2",
            "ide2": "local:iso/seed.iso,media=cdrom",  # slot 2 = seed CDROM
        }
        # ``-data1`` resolves to slot 2, which only has the cdrom here.
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web-data1.qcow2")
        with pytest.raises(DriverError, match="no disk at slot 2"):
            _storage.download_from_pool(c, ref, Path("/tmp/out.qcow2"))
        assert c.got == []  # nothing was downloaded


class TestDeleteAndPools:
    def test_delete_volume_removes_named_content(self) -> None:
        c = _client()
        ref = _naming.compose_volume_ref("local", "tr-pool-x-p1", "tr-vm-x-web.qcow2")
        c.api.content_vols = [{"volid": str(ref)}]  # present → delete fires
        _storage.delete_volume(c, ref)
        assert c.api.deleted == [str(ref)]

    def test_delete_volume_tolerates_absence(self) -> None:
        # PVE-26: absence is established by a listing membership check (the volume
        # isn't there), NOT by swallowing the delete's failure — so no delete is
        # even attempted, and a later real failure can't hide behind "already gone".
        c = _client()  # content listing is empty by default → ref is absent
        _storage.delete_volume(c, VolumeRef("local:import/tr-pool-x-p1__gone.qcow2"))
        assert c.api.deleted == []  # gone → no-op, no delete call

    def test_delete_volume_propagates_real_error(self) -> None:
        # PVE-26: a present volume whose delete fails (perms, in-use, API outage)
        # must surface — swallowing it would let teardown forget+leak the resource.
        c = _client()
        ref = VolumeRef("local:import/tr-pool-x-p1__stuck.qcow2")
        c.api.content_vols = [{"volid": str(ref)}]  # present
        c.api.delete_raises = True
        with pytest.raises(RuntimeError, match="simulated 404"):
            _storage.delete_volume(c, ref)

    def test_destroy_pool_sweeps_prefixed_volumes(self) -> None:
        c = _client()
        c.api.content_vols = [
            {"volid": "local:import/tr-pool-x-p1__a.qcow2"},
            {"volid": "local:iso/tr-pool-x-p1__seed.iso"},
            {"volid": "local:import/other-pool__b.qcow2"},
        ]
        _storage.destroy_pool(c, "tr-pool-x-p1")
        assert set(c.api.deleted) == {
            "local:import/tr-pool-x-p1__a.qcow2",
            "local:iso/tr-pool-x-p1__seed.iso",
        }

    def test_blank_and_resize_are_deferred_noops(self) -> None:
        ref = VolumeRef("local:import/tr-pool-x-p1__d.qcow2")
        assert _storage.create_blank_volume(ref, 32) == ref
        assert _storage.resize_volume(ref, 8) == ref
