"""VM lifecycle + serial build-result sink for the libvirt backend (BACKEND-1.B).

Driven against duck-typed fakes — no daemon, ``libvirt`` only as a monkeypatched
constants namespace for the lifecycle calls. Covers domain-XML synthesis (the
device shape the guest depends on), the define/start/shutdown/destroy/state
lifecycle, and the serial sink's heartbeat/EOF contract.
"""

from __future__ import annotations

import socket
from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from testrange.devices import CPU, HardDrive, Memory, OSDrive
from testrange.devices.network import NetworkIface
from testrange.drivers.base import VolumeRef
from testrange.drivers.libvirt import _serial, _vm
from testrange.exceptions import DriverError
from testrange.vms.spec import VMSpec


class _FakeLibvirtError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"libvirt error {code}")
        self._code = code

    def get_error_code(self) -> int:
        return self._code


def _fake_libvirt() -> SimpleNamespace:
    return SimpleNamespace(
        VIR_DOMAIN_RUNNING=1,
        VIR_DOMAIN_SHUTOFF=5,
        VIR_DOMAIN_UNDEFINE_MANAGED_SAVE=1,
        VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA=2,
        VIR_DOMAIN_UNDEFINE_NVRAM=4,
        VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA=16,
        VIR_ERR_NO_DOMAIN_SNAPSHOT=72,
        libvirtError=_FakeLibvirtError,
    )


class FakeSnap:
    def __init__(self, name: str, ctime: int) -> None:
        self._name = name
        self._ctime = ctime
        self.deleted = False

    def getName(self) -> str:
        return self._name

    def getXMLDesc(self, flags: int) -> str:
        return f"<domainsnapshot><creationTime>{self._ctime}</creationTime></domainsnapshot>"

    def delete(self, flags: int) -> None:
        self.deleted = True


class FakeVol:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class FakeDomain:
    def __init__(self, name: str, *, state: int = 5) -> None:
        self.name = name
        self._state = state
        self.ops: list[str] = []
        self.undefine_flags: int | None = None
        self.snaps: list[FakeSnap] = []
        self.snap_xml: list[str] = []
        self.reverted_to: str | None = None

    def isActive(self) -> bool:
        return self._state == 1

    def create(self) -> None:
        self._state = 1
        self.ops.append("create")

    def shutdown(self) -> None:
        self.ops.append("shutdown")
        self._state = 5  # fake guest powers off immediately

    def destroy(self) -> None:
        self._state = 5
        self.ops.append("destroy")

    def undefineFlags(self, flags: int) -> None:
        self.undefine_flags = flags
        self.ops.append("undefine")

    def state(self) -> list[int]:
        return [self._state, 0]

    def snapshotCreateXML(self, xml: str, flags: int) -> FakeSnap:
        name = xml.split("<name>", 1)[1].split("</name>", 1)[0]
        snap = FakeSnap(name, ctime=len(self.snaps))
        self.snaps.append(snap)
        self.snap_xml.append(xml)
        return snap

    def snapshotLookupByName(self, name: str) -> FakeSnap:
        for s in self.snaps:
            if s.getName() == name:
                return s
        raise _FakeLibvirtError(72)  # VIR_ERR_NO_DOMAIN_SNAPSHOT

    def listAllSnapshots(self, flags: int) -> list[FakeSnap]:
        return list(self.snaps)

    def revertToSnapshot(self, snap: FakeSnap, flags: int) -> None:
        self.reverted_to = snap.getName()


class FakeConn:
    def __init__(self) -> None:
        self.defined_xml: list[str] = []

    def defineXML(self, xml: str) -> FakeDomain:
        self.defined_xml.append(xml)
        return FakeDomain("d")


class FakeClient:
    def __init__(self) -> None:
        self.conn = FakeConn()
        self.vols: dict[str, FakeVol] = {}
        self.domains: dict[str, FakeDomain] = {}
        self.serial_opened: list[str] = []
        self.serial_closed: list[str] = []
        self._accept: Any = None

    @property
    def raw(self) -> FakeConn:
        return self.conn

    def lookup_volume(self, pool: str, name: str) -> FakeVol | None:
        return self.vols.get(f"{pool}/{name}")

    def lookup_domain(self, name: str) -> FakeDomain | None:
        return self.domains.get(name)

    def open_serial_listener(self, backend_name: str) -> str:
        self.serial_opened.append(backend_name)
        return f"/run/tr/{backend_name}.sock"

    def close_serial_listener(self, backend_name: str) -> None:
        self.serial_closed.append(backend_name)

    def accept_serial(self, backend_name: str, *, timeout: float) -> Any:
        if self._accept is None:
            raise AssertionError("no accept fake set")
        return self._accept


def _spec(name: str = "web", *, nics: int = 0, data: int = 0) -> VMSpec:
    devs: list[Any] = [CPU(2), Memory(1024), OSDrive("pool1", 8)]
    devs += [HardDrive("pool1", 2) for _ in range(data)]
    devs += [NetworkIface(f"net{i}") for i in range(nics)]
    return VMSpec(name=name, devices=devs)


class TestDomainXML:
    def test_disks_nics_serial_and_qga(self) -> None:
        xml = _vm._domain_xml(
            "tr-vm-x-web",
            _spec(nics=2, data=2),
            os_path="/p/os.qcow2",
            data_paths=["/p/b.qcow2", "/p/c.qcow2"],
            seed_path="/p/seed.iso",
            nics=[("02:aa:bb:cc:dd:01", "net-a"), ("02:aa:bb:cc:dd:02", "net-b")],
            serial_sock="/run/tr/web.sock",
        )
        assert "<name>tr-vm-x-web</name>" in xml
        assert "<memory unit='MiB'>1024</memory>" in xml and "<vcpu>2</vcpu>" in xml
        # OS at vda, data at vdb/vdc (the fileserver capability depends on order).
        assert "dev='vda'" in xml and "dev='vdb'" in xml and "dev='vdc'" in xml
        assert "device='cdrom'" in xml and "/p/seed.iso" in xml
        assert xml.count("<interface type='network'>") == 2
        assert "org.qemu.guest_agent.0" in xml
        assert "<serial type='unix'>" in xml and "mode='connect'" in xml
        assert "<acpi/>" in xml  # graceful shutdown needs ACPI
        # A VGA device is mandatory: without it (under libvirt's -nodefaults) the
        # Debian cloud image's GRUB gfxterm loops and never boots the kernel.
        assert "<video><model type='vga'/></video>" in xml

    def test_run_vm_gets_pty_serial_no_seed(self) -> None:
        xml = _vm._domain_xml(
            "tr-vm-x-web",
            _spec(),
            os_path="/p/os.qcow2",
            data_paths=[],
            seed_path=None,
            nics=[],
            serial_sock=None,
        )
        assert "<serial type='pty'>" in xml
        assert "device='cdrom'" not in xml
        assert "<interface" not in xml


class TestCreateVM:
    def test_build_vm_defines_and_opens_serial(self) -> None:
        client = FakeClient()
        client.vols["bp/tr-vm-x-web.qcow2"] = FakeVol("/img/os.qcow2")
        client.vols["bp/seed.iso"] = FakeVol("/img/seed.iso")
        _vm.create_vm(
            client,  # type: ignore[arg-type]
            "tr-vm-x-web",
            _spec(),
            "plan",
            os_disk_ref=VolumeRef("bp/tr-vm-x-web.qcow2"),
            seed_iso_ref=VolumeRef("bp/seed.iso"),
            network_refs={},
        )
        assert client.serial_opened == ["tr-vm-x-web"]  # seed VM => serial listener
        assert "mode='connect' path=\"/run/tr/tr-vm-x-web.sock\"" in client.conn.defined_xml[0]

    def test_run_vm_no_serial_listener(self) -> None:
        client = FakeClient()
        client.vols["p/os.qcow2"] = FakeVol("/img/os.qcow2")
        _vm.create_vm(
            client,  # type: ignore[arg-type]
            "tr-vm-x-web",
            _spec(),
            "plan",
            os_disk_ref=VolumeRef("p/os.qcow2"),
            seed_iso_ref=None,
            network_refs={},
        )
        assert client.serial_opened == []

    def test_missing_volume_raises(self) -> None:
        client = FakeClient()
        with pytest.raises(DriverError, match="no volume"):
            _vm.create_vm(
                client,  # type: ignore[arg-type]
                "tr-vm-x-web",
                _spec(),
                "plan",
                os_disk_ref=VolumeRef("p/missing.qcow2"),
                seed_iso_ref=None,
                network_refs={},
            )


class TestLifecycle:
    def test_start_only_if_not_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeClient()
        dom = FakeDomain("tr-vm-x-web", state=5)
        client.domains["tr-vm-x-web"] = dom
        _vm.start_vm(client, "tr-vm-x-web")  # type: ignore[arg-type]
        assert dom.ops == ["create"] and dom.isActive()

    def test_shutdown_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        dom = FakeDomain("tr-vm-x-web", state=1)
        client.domains["tr-vm-x-web"] = dom
        _vm.shutdown_vm(client, "tr-vm-x-web", timeout=5.0)  # type: ignore[arg-type]
        assert "shutdown" in dom.ops and dom.state()[0] == 5

    def test_destroy_undefines_with_flags_and_closes_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        dom = FakeDomain("tr-vm-x-web", state=1)
        client.domains["tr-vm-x-web"] = dom
        _vm.destroy_vm(client, "tr-vm-x-web")  # type: ignore[arg-type]
        assert dom.ops == ["destroy", "undefine"]
        assert dom.undefine_flags == 1 | 2 | 16 | 4  # snapshots/checkpoints/nvram cleared
        assert client.serial_closed == ["tr-vm-x-web"]

    def test_destroy_absent_is_noop_but_releases_serial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        _vm.destroy_vm(client, "ghost")  # type: ignore[arg-type]
        assert client.serial_closed == ["ghost"]

    def test_power_state_maps_to_vocab(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        client.domains["r"] = FakeDomain("r", state=1)
        client.domains["s"] = FakeDomain("s", state=5)
        assert _vm.get_vm_power_state(client, "r") == "running"  # type: ignore[arg-type]
        assert _vm.get_vm_power_state(client, "s") == "shutoff"  # type: ignore[arg-type]

    def test_resolve_missing_domain_raises(self) -> None:
        with pytest.raises(DriverError, match="no libvirt domain"):
            _vm.start_vm(FakeClient(), "ghost")  # type: ignore[arg-type]


class _FakeSock:
    """A socketpair-like fake: replays scripted recv results."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.closed = False

    def settimeout(self, t: float) -> None:
        pass

    def recv(self, n: int) -> bytes:
        item = self._script.pop(0)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        assert isinstance(item, bytes)
        return item

    def close(self) -> None:
        self.closed = True


class TestSerialSink:
    def test_yields_chunks_heartbeat_then_eof(self) -> None:
        client = FakeClient()
        client._accept = _FakeSock([b"boot...", TimeoutError, b"TESTRANGE-RESULT: ok\n", b""])
        got = list(_serial.read_build_result_sink(client, "tr-vm-x-web"))  # type: ignore[arg-type]
        assert got == [b"boot...", b"", b"TESTRANGE-RESULT: ok\n"]

    def test_closes_socket_on_early_break(self) -> None:
        client = FakeClient()
        sock = _FakeSock([b"a", b"b", b""])
        client._accept = sock
        with closing(_serial.read_build_result_sink(client, "x")) as gen:  # type: ignore[arg-type]
            next(gen)  # one chunk, then break early
        assert sock.closed

    def test_accept_timeout_raises(self) -> None:
        client = FakeClient()

        def _boom(backend_name: str, *, timeout: float) -> Any:
            raise TimeoutError

        client.accept_serial = _boom  # type: ignore[method-assign]
        gen = _serial.read_build_result_sink(client, "x")  # type: ignore[arg-type]
        with pytest.raises(DriverError, match="never connected"):
            next(gen)


def test_socketpair_roundtrip_real_socket() -> None:
    """A real AF_UNIX socketpair through the sink, to exercise true recv()."""
    a, b = socket.socketpair()
    client = FakeClient()
    client._accept = a
    b.sendall(b"hello-serial")
    b.close()  # EOF after the bytes
    got = b"".join(_serial.read_build_result_sink(client, "x"))  # type: ignore[arg-type]
    assert got == b"hello-serial"


class TestSnapshots:
    def test_xml_name_and_escaped_description(self) -> None:
        xml = _vm._snapshot_xml("snap1", "before & after")
        assert "<name>snap1</name>" in xml
        assert "before &amp; after" in xml  # description escaped
        # libvirt decides memory-vs-disk-only by run state; we never opt out.
        assert "<memory" not in xml

    def test_create_then_duplicate_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        dom = FakeDomain("vm", state=1)
        client.domains["vm"] = dom
        _vm.create_snapshot(client, "vm", "s1", "d", mem=True)  # type: ignore[arg-type]
        assert dom.snaps[0].getName() == "s1" and "<memory" not in dom.snap_xml[0]
        with pytest.raises(DriverError, match="already exists"):
            _vm.create_snapshot(client, "vm", "s1")  # type: ignore[arg-type]

    def test_list_is_oldest_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        dom = FakeDomain("vm", state=1)
        client.domains["vm"] = dom
        for n in ("a", "b", "c"):
            _vm.create_snapshot(client, "vm", n)  # type: ignore[arg-type]
        assert _vm.list_snapshots(client, "vm") == ["a", "b", "c"]  # type: ignore[arg-type]

    def test_delete_tolerates_absence_restore_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        dom = FakeDomain("vm", state=1)
        client.domains["vm"] = dom
        _vm.create_snapshot(client, "vm", "s1")  # type: ignore[arg-type]
        _vm.delete_snapshot(client, "vm", "ghost")  # type: ignore[arg-type]  # no raise
        _vm.delete_snapshot(client, "vm", "s1")  # type: ignore[arg-type]
        assert dom.snaps[0].deleted
        _vm.restore_snapshot(client, "vm", "s1")  # type: ignore[arg-type]  # still resolvable
        assert dom.reverted_to == "s1"
        with pytest.raises(DriverError, match="not found"):
            _vm.restore_snapshot(client, "vm", "ghost")  # type: ignore[arg-type]
