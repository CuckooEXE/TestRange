"""PVE-6: stamped-name → vmid resolution and the Option-2 disk re-resolution.

Pure naming parsers (``_naming``) plus the live-list resolution (``_vm``),
exercised against a tiny duck-typed fake client — no proxmoxer, no real PVE.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from testrange.devices import CPU, DHCPAddr, Memory, OSDrive
from testrange.devices.network import NetworkIface
from testrange.drivers.base import VolumeRef
from testrange.drivers.proxmox import _naming, _vm
from testrange.exceptions import DriverError
from testrange.vms import VMSpec


class _FakeNodes:
    def __init__(self, vms: list[dict[str, Any]]) -> None:
        self._vms = vms

    def __call__(self, node: str) -> _FakeNodes:
        return self

    @property
    def qemu(self) -> _FakeNodes:
        return self

    def get(self) -> list[dict[str, Any]]:
        return self._vms


class _FakeApi:
    def __init__(self, vms: list[dict[str, Any]]) -> None:
        self.nodes = _FakeNodes(vms)


class _FakeClient:
    def __init__(self, vms: list[dict[str, Any]], node: str = "ns1001849") -> None:
        self.api = _FakeApi(vms)
        self.node = node


def _client(*vms: tuple[int, str]) -> Any:
    return _FakeClient([{"vmid": vmid, "name": name} for vmid, name in vms])


def _disk_ref(vol_name: str, pool: str = "tr-pool-ab12cd-p1") -> str:
    return _naming.compose_volume_ref("local", pool, vol_name)


class TestNamingParsers:
    def test_parse_disk_ref_roundtrips_compose(self) -> None:
        ref = _naming.compose_volume_ref("local", "pool1", "tr-vm-ab12cd-web.qcow2")
        assert _naming.parse_disk_ref(ref) == ("pool1", "tr-vm-ab12cd-web.qcow2")

    def test_disk_scsi_index_os_is_zero(self) -> None:
        assert _naming.disk_scsi_index("tr-vm-x-web.qcow2", "tr-vm-x-web") == 0

    def test_disk_scsi_index_data_is_offset_by_one(self) -> None:
        assert _naming.disk_scsi_index("tr-vm-x-web-data0.qcow2", "tr-vm-x-web") == 1
        assert _naming.disk_scsi_index("tr-vm-x-web-data2.qcow2", "tr-vm-x-web") == 3

    def test_disk_scsi_index_none_for_other_vm(self) -> None:
        assert _naming.disk_scsi_index("tr-vm-x-web.qcow2", "tr-vm-x-db") is None


class TestResolveVmid:
    def test_maps_name_to_vmid(self) -> None:
        c = _client((100, "tr-vm-x-web"), (101, "tr-vm-x-db"))
        assert _vm.resolve_vmid(c, "tr-vm-x-db") == 101

    def test_list_vms_skips_nameless(self) -> None:
        c: Any = _FakeClient([{"vmid": 100, "name": "tr-vm-x-web"}, {"vmid": 9}])
        assert _vm.list_vms(c) == {"tr-vm-x-web": 100}

    def test_missing_name_raises(self) -> None:
        c = _client((100, "tr-vm-x-web"))
        with pytest.raises(DriverError, match="no PVE VM named"):
            _vm.resolve_vmid(c, "tr-vm-x-nope")


class TestResolveDisk:
    def test_os_disk_resolves_to_scsi0(self) -> None:
        c = _client((100, "tr-vm-x-web"))
        assert _vm.resolve_disk(c, _disk_ref("tr-vm-x-web.qcow2")) == (100, 0)

    def test_data_disk_resolves_to_offset_scsi(self) -> None:
        c = _client((100, "tr-vm-x-web"))
        assert _vm.resolve_disk(c, _disk_ref("tr-vm-x-web-data1.qcow2")) == (100, 2)

    def test_longest_prefix_wins_when_names_overlap(self) -> None:
        # Both "web" and "web-data0" are real VMs; the disk "web-data0.qcow2"
        # belongs to the VM literally named "...web-data0" (its OS disk), not to
        # "...web" (which would read it as data disk 1).
        c = _client((100, "tr-vm-x-web"), (101, "tr-vm-x-web-data0"))
        assert _vm.resolve_disk(c, _disk_ref("tr-vm-x-web-data0.qcow2")) == (101, 0)

    def test_unowned_disk_raises(self) -> None:
        c = _client((100, "tr-vm-x-web"))
        with pytest.raises(DriverError, match="no PVE VM owns disk ref"):
            _vm.resolve_disk(c, _disk_ref("tr-vm-x-ghost.qcow2"))


class TestCreateVmDiskFormat:
    def test_installer_origin_os_disk_is_qcow2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-59: an installer-origin OS disk must carry ,format=qcow2 like the
        # sibling blank data disks; a bare `local:8` allocates RAW on a dir store,
        # which the build then caches under a .qcow2 name (format/label mismatch).
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            _vm, "_post_new_vm", lambda _client, config, **_kw: captured.update(config) or 100
        )
        monkeypatch.setattr(_vm, "_wait_unlocked", lambda *_a, **_k: None)
        client = SimpleNamespace(storage="local")
        spec = VMSpec(
            name="web",
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                NetworkIface("netA", addr=DHCPAddr()),
            ],
        )
        _vm.create_vm(
            cast(Any, client),
            "tr-vm-x-web",
            spec,
            "plan",
            os_disk_ref=VolumeRef("unused"),
            seed_iso_ref=None,
            network_refs={"netA": "vmbr0"},
            boot_media_ref=VolumeRef("local:iso/pve.iso"),
        )
        assert captured["scsi0"] == "local:8,format=qcow2"
