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
from testrange.handles import NetworkHandle, PoolHandle
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
                OSDrive(PoolHandle("pool1"), 8),
                NetworkIface(NetworkHandle("netA", switch="sw1"), addr=DHCPAddr()),
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


class _ShutdownEndpoint:
    def __init__(self, api: _ShutdownApi, path: str) -> None:
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "post"):
            return lambda **kw: self._api._call(name, self._path, kw)
        return _ShutdownEndpoint(self._api, f"{self._path}/{name}")

    def __call__(self, *args: Any) -> _ShutdownEndpoint:
        return _ShutdownEndpoint(self._api, f"{self._path}/{'/'.join(str(a) for a in args)}")


class _ShutdownApi:
    """Chained fake covering exactly the shutdown path: stamped-name resolution
    (``qemu.get``) plus the shutdown POST, whose task outcome models the PVE-61
    lingering mem-snapshot config flock."""

    def __init__(self, vms: list[dict[str, Any]]) -> None:
        self._vms = vms
        self.shutdown_posts: list[dict[str, Any]] = []
        # The shutdown task hits the transient per-VM config flock this many
        # times (UPID -> wait_task raises "got timeout" lock), then succeeds;
        # hard_fail models a genuine, non-retryable failure.
        self.lock_fails = 0
        self.hard_fail = False

    def __getattr__(self, name: str) -> _ShutdownEndpoint:
        return _ShutdownEndpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        if path.endswith("/qemu") and method == "get":
            return self._vms
        if path.endswith("/status/shutdown") and method == "post":
            self.shutdown_posts.append(kwargs)
            if self.hard_fail:
                return "UPID:shutdown-hard-doomed"
            if self.lock_fails > 0:
                self.lock_fails -= 1
                return "UPID:shutdown-lock-doomed"
            return "UPID:shutdown-ok"
        raise AssertionError(f"unexpected API call: {method} {path} {kwargs}")


class _ShutdownClient:
    def __init__(self, vms: list[dict[str, Any]], node: str = "ns1001849") -> None:
        self.api = _ShutdownApi(vms)
        self.node = node
        self.waited: list[tuple[str, float]] = []

    def wait_task(self, upid: str, *, timeout: float = 600.0) -> None:
        self.waited.append((upid, timeout))
        if upid == "UPID:shutdown-lock-doomed":
            raise DriverError(
                "PVE task failed: exitstatus=\"can't lock file "
                "'/var/lock/qemu-server/lock-100.conf' - got timeout\""
            )
        if upid == "UPID:shutdown-hard-doomed":
            raise DriverError('PVE task failed: exitstatus="internal error"')


class TestShutdownLockRetry:
    def _client(self) -> Any:
        return _ShutdownClient([{"vmid": 100, "name": "tr-vm-x-web"}])

    def test_shutdown_retries_transient_config_flock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-61: a mem=True snapshot op holds the per-VM config flock past its
        # task's completion, and a shutdown can be the very next driver call
        # (snapshot_chain, BACKEND-14) — it must ride the flock out like
        # rollback/delete do.
        monkeypatch.setattr("time.sleep", lambda _s: None)  # skip backoff
        c = self._client()
        c.api.lock_fails = 2  # two transient flock failures, then success
        _vm.shutdown_vm(c, "tr-vm-x-web", timeout=30.0)
        assert c.api.lock_fails == 0  # retried past the flock
        assert c.api.shutdown_posts == [{"timeout": 30, "forceStop": 1}] * 3
        assert c.waited[-1] == ("UPID:shutdown-ok", 60.0)  # margin-await, not plain _await

    def test_shutdown_does_not_retry_non_lock_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A genuine (non-flock) task failure propagates immediately, no retry.
        monkeypatch.setattr("time.sleep", lambda _s: None)
        c = self._client()
        c.api.hard_fail = True
        with pytest.raises(DriverError, match="internal error"):
            _vm.shutdown_vm(c, "tr-vm-x-web", timeout=30.0)
        assert len(c.api.shutdown_posts) == 1  # issued once, no retry
