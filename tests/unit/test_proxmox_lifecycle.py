"""PVE-8: VM lifecycle (create_vm + start/shutdown/destroy/power-state).

A chained fake API records the proxmoxer calls so we can assert the exact PVE
config create_vm composes (import-from OS, blank vs import data disks, seed
CDROM, stable-MAC NICs, agent) and the lifecycle task calls — no proxmoxer, no
real PVE.
"""

from __future__ import annotations

from typing import Any

import pytest

from testrange.devices import CPU, HardDrive, Memory, OSDrive
from testrange.devices.network import NetworkIface
from testrange.drivers.base import VolumeRef
from testrange.drivers.proxmox import _naming, _vm
from testrange.exceptions import DriverError
from testrange.vms import VMSpec


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
        self.nextid = 100
        self.vms: list[dict[str, Any]] = []
        self.status = "running"
        self.existing_content: set[str] = set()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.created: dict[str, Any] = {}
        self.resized: dict[str, Any] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.lock_remaining = 0  # config reports a 'lock' for this many polls, then clears
        self.resize_fails = 0  # resize task fails transiently this many times, then succeeds

    def __getattr__(self, name: str) -> _Endpoint:
        return _Endpoint(self, name)

    def _call(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, path, kwargs))
        if path == "cluster/nextid" and method == "get":
            return str(self.nextid)
        if path.endswith("/qemu") and method == "get":
            return self.vms
        if path.endswith("/qemu") and method == "post":
            self.created = kwargs
            return None
        if path.endswith("/status/current") and method == "get":
            return {"status": self.status}
        if "/status/" in path and method == "post":  # start / stop / shutdown
            return None
        if path.endswith("/resize") and method == "put":
            if self.resize_fails > 0:
                self.resize_fails -= 1
                return "UPID:resize-doomed"  # _FakeClient.wait_task raises on this
            self.resized = kwargs
            return None
        if path.endswith("/config") and method == "get":
            if self.lock_remaining > 0:
                self.lock_remaining -= 1
                return {"lock": "create"}
            return {}
        if path.endswith("/content") and method == "get":
            # PVE-26: existence is tested by listing membership, not a single-volid
            # probe — so a real API error propagates instead of reading as absence.
            return [{"volid": v} for v in self.existing_content]
        if path.endswith("/snapshot") and method == "get":
            return self.snapshots
        if path.endswith("/snapshot") and method == "post":
            self.snapshots.append({"name": kwargs["snapname"], "snaptime": len(self.snapshots)})
            return None
        if path.endswith("/rollback") and method == "post":
            return None
        if "/snapshot/" in path and method == "delete":
            name = path.split("/snapshot/", 1)[1]
            self.snapshots = [s for s in self.snapshots if s["name"] != name]
            return None
        if "/qemu/" in path and method == "delete":
            return None
        raise AssertionError(f"unexpected API call: {method} {path} {kwargs}")


class _FakeClient:
    def __init__(self, node: str = "ns1001849", storage: str = "local") -> None:
        self.api = _FakeApi()
        self.node = node
        self.storage = storage
        self.waited: list[str] = []

    def wait_task(self, upid: str, *, timeout: float = 600.0) -> None:
        self.waited.append(upid)
        if upid == "UPID:resize-doomed":
            raise DriverError("resize task failed: got timeout")


def _client() -> Any:
    return _FakeClient()


def _spec(*, data: int = 0, nics: tuple[str, ...] = ("netA",)) -> VMSpec:
    devices: list[Any] = [CPU(2), Memory(1024), OSDrive("pool1", 8)]
    devices += [HardDrive("pool1", 32) for _ in range(data)]
    devices += [NetworkIface(n) for n in nics]
    return VMSpec(name="web", devices=devices)


_OS_REF = VolumeRef("local:import/p1__tr-vm-x-web.qcow2")
_SEED = VolumeRef("local:iso/p1__tr-vm-x-web-seed.iso")


def _create(c: Any, **kw: Any) -> str:
    base: dict[str, Any] = {
        "os_disk_ref": _OS_REF,
        "seed_iso_ref": None,
        "network_refs": {"netA": "vneta"},
    }
    base.update(kw)
    return _vm.create_vm(c, "tr-vm-x-web", _spec(), "plan", **base)


class TestCreateVm:
    def test_os_disk_imports_from_staging_and_stamps_name(self) -> None:
        c = _client()
        _create(c)
        cfg = c.api.created
        assert cfg["name"] == "tr-vm-x-web"  # the PVE-6 resolution stamp
        assert cfg["scsi0"] == f"local:0,import-from={_OS_REF}"
        assert cfg["agent"] == 1 and cfg["scsihw"] == "virtio-scsi-single"
        assert cfg["cores"] == 2 and cfg["memory"] == 1024

    def test_seed_attaches_cdrom_and_grows_os_disk(self) -> None:
        c = _client()
        _create(c, seed_iso_ref=_SEED)
        assert c.api.created["ide2"] == f"{_SEED},media=cdrom"
        assert c.api.resized == {"disk": "scsi0", "size": "8G"}  # grown to spec

    def test_seedless_run_vm_is_not_resized(self) -> None:
        c = _client()
        _create(c, seed_iso_ref=None)
        assert c.api.resized == {}  # cached run disk is already full-size

    def test_create_waits_for_config_lock_to_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-12: import-from/resize hold the config lock; create_vm must poll
        # until it clears so the orchestrator's start_vm doesn't race it.
        monkeypatch.setattr(_vm, "_POLL_INTERVAL_S", 0)
        c = _client()
        c.api.lock_remaining = 3
        _create(c, seed_iso_ref=_SEED)
        assert c.api.lock_remaining == 0  # polled config until unlocked

    def test_resize_retries_transient_image_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # PVE-12: after import-from, qemu-img can't lock the image for a few
        # seconds, so the resize task fails with "got timeout"; create_vm retries.
        monkeypatch.setattr("time.sleep", lambda _s: None)  # skip backoff
        c = _client()
        c.api.resize_fails = 2  # two transient failures, then success
        _create(c, seed_iso_ref=_SEED)
        assert c.api.resized == {"disk": "scsi0", "size": "8G"}  # eventually applied
        assert c.api.resize_fails == 0

    def test_build_data_disk_is_blank_sized_from_spec(self) -> None:
        c = _client()
        _vm.create_vm(
            c,
            "tr-vm-x-web",
            _spec(data=1),
            "plan",
            os_disk_ref=_OS_REF,
            seed_iso_ref=_SEED,  # seed present → build phase
            network_refs={"netA": "vneta"},
            data_disk_refs=[VolumeRef("local:import/p1__tr-vm-x-web-data0.qcow2")],
        )
        assert c.api.created["scsi1"] == "local:32"  # blank, sized from HardDrive

    def test_run_data_disk_imports_from_staging(self) -> None:
        c = _client()
        data_ref = VolumeRef("local:import/p1__tr-vm-x-web-data0.qcow2")
        _vm.create_vm(
            c,
            "tr-vm-x-web",
            _spec(data=1),
            "plan",
            os_disk_ref=_OS_REF,
            seed_iso_ref=None,  # no seed → run phase: import the cached disk
            network_refs={"netA": "vneta"},
            data_disk_refs=[data_ref],
        )
        assert c.api.created["scsi1"] == f"local:0,import-from={data_ref}"

    def test_data_disk_intent_follows_seed_not_staging(self) -> None:
        # PVE-27: build-vs-run is decided by the orchestrator's intent (seed
        # presence), NOT by probing whether the staging volume exists. A leftover
        # staging volume from a crashed prior build must not flip a build-phase
        # blank into a stale import.
        c = _client()
        data_ref = VolumeRef("local:import/p1__tr-vm-x-web-data0.qcow2")
        c.api.existing_content.add(str(data_ref))  # stale staging present...
        _vm.create_vm(
            c,
            "tr-vm-x-web",
            _spec(data=1),
            "plan",
            os_disk_ref=_OS_REF,
            seed_iso_ref=_SEED,  # ...but seed present → still build, ignore the staging
            network_refs={"netA": "vneta"},
            data_disk_refs=[data_ref],
        )
        assert c.api.created["scsi1"] == "local:32"  # blank, stale staging ignored

    def test_nics_get_stable_macs_on_their_bridges(self) -> None:
        c = _client()
        _vm.create_vm(
            c,
            "tr-vm-x-web",
            _spec(nics=("netA", "netB")),
            "plan",
            os_disk_ref=_OS_REF,
            seed_iso_ref=None,
            network_refs={"netA": "vneta", "netB": "vnetb"},
        )
        mac0 = _naming.compose_mac("plan", "web", 0)
        mac1 = _naming.compose_mac("plan", "web", 1)
        assert c.api.created["net0"] == f"virtio={mac0},bridge=vneta"
        assert c.api.created["net1"] == f"virtio={mac1},bridge=vnetb"


class TestLifecycle:
    def _running_vm(self) -> Any:
        c = _client()
        c.api.vms = [{"vmid": 100, "name": "tr-vm-x-web"}]
        return c

    def test_start_posts_status_start(self) -> None:
        c = self._running_vm()
        _vm.start_vm(c, "tr-vm-x-web")
        assert any(p.endswith("/qemu/100/status/start") and m == "post" for m, p, _ in c.api.calls)

    def test_shutdown_is_graceful_then_forced(self) -> None:
        c = self._running_vm()
        _vm.shutdown_vm(c, "tr-vm-x-web", timeout=30.0)
        call = next(kw for m, p, kw in c.api.calls if p.endswith("/status/shutdown"))
        assert call == {"timeout": 30, "forceStop": 1}

    def test_destroy_stops_then_purges(self) -> None:
        c = self._running_vm()
        _vm.destroy_vm(c, "tr-vm-x-web")
        methods = [(m, p.split("/")[-1], kw) for m, p, kw in c.api.calls]
        assert ("post", "stop", {}) in methods
        # purge=1 + destroy-unreferenced-disks=1 on the delete
        delete = next(kw for m, p, kw in c.api.calls if m == "delete")
        assert delete == {"purge": 1, "destroy-unreferenced-disks": 1}

    def test_power_state_maps_stopped_to_shutoff(self) -> None:
        c = self._running_vm()
        c.api.status = "stopped"
        assert _vm.get_vm_power_state(c, "tr-vm-x-web") == "shutoff"
        c.api.status = "running"
        assert _vm.get_vm_power_state(c, "tr-vm-x-web") == "running"


class TestSnapshots:
    def _vm(self) -> Any:
        c = _client()
        c.api.vms = [{"vmid": 100, "name": "tr-vm-x-web"}]
        return c

    def test_create_memory_snapshot_sets_vmstate(self) -> None:
        c = self._vm()
        _vm.create_snapshot(c, "tr-vm-x-web", "s1", "before", mem=True)
        post = next(kw for m, p, kw in c.api.calls if p.endswith("/snapshot") and m == "post")
        assert post == {"snapname": "s1", "description": "before", "vmstate": 1}

    def test_disk_only_snapshot_has_vmstate_zero(self) -> None:
        c = self._vm()
        _vm.create_snapshot(c, "tr-vm-x-web", "s1")
        post = next(kw for m, p, kw in c.api.calls if p.endswith("/snapshot") and m == "post")
        assert post["vmstate"] == 0

    def test_duplicate_snapshot_raises(self) -> None:
        c = self._vm()
        c.api.snapshots = [{"name": "s1", "snaptime": 1}]
        with pytest.raises(DriverError, match="already exists"):
            _vm.create_snapshot(c, "tr-vm-x-web", "s1")

    def test_list_excludes_current_and_is_ordered(self) -> None:
        c = self._vm()
        c.api.snapshots = [
            {"name": "b", "snaptime": 2},
            {"name": "current", "snaptime": 9},
            {"name": "a", "snaptime": 1},
        ]
        assert _vm.list_snapshots(c, "tr-vm-x-web") == ["a", "b"]

    def test_delete_missing_is_noop(self) -> None:
        c = self._vm()
        _vm.delete_snapshot(c, "tr-vm-x-web", "ghost")
        assert not any(m == "delete" for m, _p, _kw in c.api.calls)

    def test_restore_missing_raises(self) -> None:
        c = self._vm()
        with pytest.raises(DriverError, match="not found"):
            _vm.restore_snapshot(c, "tr-vm-x-web", "ghost")

    def test_restore_existing_rolls_back(self) -> None:
        c = self._vm()
        c.api.snapshots = [{"name": "s1", "snaptime": 1}]
        _vm.restore_snapshot(c, "tr-vm-x-web", "s1")
        assert any(p.endswith("/snapshot/s1/rollback") and m == "post" for m, p, _ in c.api.calls)
