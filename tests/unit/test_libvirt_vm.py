"""VM lifecycle + serial build-result sink for the libvirt backend (BACKEND-1.B).

Driven against duck-typed fakes — no daemon, ``libvirt`` only as a monkeypatched
constants namespace. Covers domain-XML synthesis (the device shape the guest
depends on), the define/start/shutdown/destroy/state lifecycle, and the
``virDomainOpenConsole`` sink's retry/heartbeat/EOF contract (BACKEND-5).
"""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
from typing import Any

import pytest

from testrange.devices import CPU, HardDrive, Memory, OSDrive
from testrange.devices.disk.libvirt import LibvirtDataDrive, LibvirtOSDrive
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.base import VolumeRef
from testrange.drivers.libvirt import _serial, _vm
from testrange.exceptions import DriverError
from testrange.networks.base import BuildNic, NetworkAddressing
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

    @property
    def raw(self) -> FakeConn:
        return self.conn

    def lookup_volume(self, pool: str, name: str) -> FakeVol | None:
        return self.vols.get(f"{pool}/{name}")

    def lookup_domain(self, name: str) -> FakeDomain | None:
        return self.domains.get(name)


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
            nics=[
                ("02:aa:bb:cc:dd:01", "net-a", "virtio"),
                ("02:aa:bb:cc:dd:02", "net-b", "virtio"),
            ],
        )
        assert "<name>tr-vm-x-web</name>" in xml
        assert "<memory unit='MiB'>1024</memory>" in xml and "<vcpu>2</vcpu>" in xml
        # OS at vda, data at vdb/vdc (the fileserver capability depends on order).
        assert "dev='vda'" in xml and "dev='vdb'" in xml and "dev='vdc'" in xml
        assert "bus='virtio'" in xml and "<model type='virtio'/>" in xml  # default models
        assert "device='cdrom'" in xml and "/p/seed.iso" in xml
        assert xml.count("<interface type='network'>") == 2
        assert "org.qemu.guest_agent.0" in xml
        # Every guest gets a pty serial — the sink reads it via openConsole
        # (BACKEND-5), so no host-local socket path is in the XML.
        assert "<serial type='pty'>" in xml
        assert "<serial type='unix'>" not in xml
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
        )
        assert "<serial type='pty'>" in xml
        assert "device='cdrom'" not in xml
        assert "<interface" not in xml


class TestLibvirtDeviceVariants:
    """ESXi-shaped guest: sata/ide disk + e1000e NIC, via the libvirt variants."""

    def test_sata_os_disk_and_e1000e_nic(self) -> None:
        spec = VMSpec(
            name="esxi",
            firmware="bios",
            devices=[
                CPU(4, nested=True),
                Memory(8192),
                LibvirtOSDrive("pool1", 33, bus="sata"),
                LibvirtNetworkIface("lab", model="e1000e"),
            ],
        )
        xml = _vm._domain_xml(
            "tr-vm-x-esxi",
            spec,
            os_path="/p/os.qcow2",
            data_paths=[],
            seed_path=None,
            boot_media_path="/p/esxi.iso",
            nics=[("02:aa:bb:cc:dd:10", "lab-net", "e1000e")],
        )
        # OS disk on sata -> sd-prefix; the installer CDROM is IDE on BIOS/i440fx
        # (ESXi weasel only finds ks= on an IDE CDROM), so they don't even share a
        # prefix namespace: OS disk sda, installer CD hda.
        assert "<target dev='sda' bus='sata'/>" in xml  # OS disk
        assert "dev='hda' bus='ide'" in xml  # installer CDROM on IDE
        assert "<model type='e1000e'/>" in xml
        # No disk hangs off virtio-blk and the NIC isn't virtio-net (the QGA
        # virtio-serial channel still carries an unrelated type='virtio').
        assert "bus='virtio'" not in xml
        assert "<model type='virtio'/>" not in xml

    def test_ide_os_disk_uses_hd_prefix(self) -> None:
        spec = VMSpec(
            name="esxi",
            devices=[CPU(2), Memory(4096), LibvirtOSDrive("pool1", 33, bus="ide")],
        )
        xml = _vm._domain_xml(
            "tr-vm-x-esxi",
            spec,
            os_path="/p/os.qcow2",
            data_paths=[],
            seed_path=None,
            boot_media_path="/p/esxi.iso",
            nics=[],
        )
        # IDE OS disk -> hda; the installer CDROM is also IDE (BIOS), so the shared
        # per-prefix allocator gives it the next hd letter (hdb) — no collision.
        assert "<target dev='hda' bus='ide'/>" in xml  # ide OS disk
        assert "dev='hdb' bus='ide'" in xml  # installer CDROM, next hd slot

    def test_uefi_os_uses_q35_efi_with_secure_boot_disabled(self) -> None:
        # A UEFI VM boots a captured installer-built disk with fresh per-domain
        # EFI vars via the removable-media fallback, which can't use Secure Boot.
        # The <os> must select firmware='efi' on q35 with secure-boot explicitly
        # off, else OVMF rejects the fallback ("prohibited by secure boot policy")
        # and the run boot never comes up (PVE-57).
        spec = VMSpec(
            name="pve",
            firmware="uefi",
            devices=[CPU(2), Memory(2048), OSDrive("pool1", 16)],
        )
        xml = _vm._domain_xml(
            "tr-vm-x-pve",
            spec,
            os_path="/p/os.qcow2",
            data_paths=[],
            seed_path=None,
            nics=[],
        )
        assert "<os firmware='efi'>" in xml
        assert "machine='q35'" in xml
        assert "<firmware><feature enabled='no' name='secure-boot'/></firmware>" in xml

    def test_libvirt_data_drive_bus_honored(self) -> None:
        spec = VMSpec(
            name="vm",
            devices=[
                CPU(2),
                Memory(1024),
                OSDrive("pool1", 8),  # plain -> virtio (vda)
                LibvirtDataDrive("pool1", 4, bus="sata"),
            ],
        )
        xml = _vm._domain_xml(
            "tr-vm-x-vm",
            spec,
            os_path="/p/os.qcow2",
            data_paths=["/p/d.qcow2"],
            seed_path=None,
            nics=[],
        )
        assert "<target dev='vda' bus='virtio'/>" in xml
        assert "<target dev='sda' bus='sata'/>" in xml  # data disk on sata


_BUILD_NIC = BuildNic(
    mac="02:aa:bb:cc:dd:fe",
    network="build-net",
    addr=StaticAddr("10.97.0.3"),
    addressing=NetworkAddressing(
        cidr="10.97.0.0/24", prefix_len=24, dhcp=True, gateway="10.97.0.1", dns_server="10.97.0.1"
    ),
)


class TestCreateVM:
    def test_build_vm_gets_pty_serial(self) -> None:
        # BACKEND-5: the build VM gets the same pty serial as every guest — no
        # host-local socket path in the XML, so the identical domain boots on a
        # local and a remote (qemu+ssh) daemon; the orchestrator reads its
        # TESTRANGE-RESULT via virDomainOpenConsole instead.
        client: Any = FakeClient()
        client.vols["bp/tr-vm-x-web.qcow2"] = FakeVol("/img/os.qcow2")
        client.vols["bp/seed.iso"] = FakeVol("/img/seed.iso")
        _vm.create_vm(
            client,
            "tr-vm-x-web",
            _spec(),
            "plan",
            os_disk_ref=VolumeRef("bp/tr-vm-x-web.qcow2"),
            seed_iso_ref=VolumeRef("bp/seed.iso"),
            network_refs={"build-net": "tr-build-net"},
            build_nic=_BUILD_NIC,
        )
        xml = client.conn.defined_xml[0]
        assert xml.count("<serial type='pty'>") == 1
        assert "<serial type='unix'>" not in xml and "mode='connect'" not in xml

    def test_build_nic_inherits_declared_e1000e_model(self) -> None:
        # The build NIC stands in for the guest's hardware: an ESXi-shaped guest
        # that declares an e1000e NIC must install over an e1000e build interface
        # (it has no virtio-net driver).
        client: Any = FakeClient()
        client.vols["bp/tr-vm-x-esxi.qcow2"] = FakeVol("/img/os.qcow2")
        spec = VMSpec(
            name="esxi",
            devices=[
                CPU(4, nested=True),
                Memory(8192),
                LibvirtOSDrive("pool1", 33, bus="sata"),
                LibvirtNetworkIface("lab", model="e1000e"),
            ],
        )
        _vm.create_vm(
            client,
            "tr-vm-x-esxi",
            spec,
            "plan",
            os_disk_ref=VolumeRef("bp/tr-vm-x-esxi.qcow2"),
            seed_iso_ref=None,
            network_refs={"build-net": "tr-build-net"},
            build_nic=_BUILD_NIC,
        )
        assert "<model type='e1000e'/>" in client.conn.defined_xml[0]

    def test_sidecar_seed_vm_gets_pty_and_seed(self) -> None:
        # A seed-carrying non-build VM (e.g. a sidecar) gets the same pty serial
        # as everyone else — undrained but harmless — with the seed CD attached.
        client: Any = FakeClient()
        client.vols["bp/tr-sidecar.qcow2"] = FakeVol("/img/os.qcow2")
        client.vols["bp/cfg.iso"] = FakeVol("/img/cfg.iso")
        _vm.create_vm(
            client,
            "tr-sidecar",
            _spec(),
            "plan",
            os_disk_ref=VolumeRef("bp/tr-sidecar.qcow2"),
            seed_iso_ref=VolumeRef("bp/cfg.iso"),
            network_refs={},
        )
        assert "<serial type='pty'>" in client.conn.defined_xml[0]
        assert "device='cdrom'" in client.conn.defined_xml[0]  # seed still attached

    def test_missing_volume_raises(self) -> None:
        client: Any = FakeClient()
        with pytest.raises(DriverError, match="no volume"):
            _vm.create_vm(
                client,
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

    def test_destroy_undefines_with_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client: Any = FakeClient()
        dom = FakeDomain("tr-vm-x-web", state=1)
        client.domains["tr-vm-x-web"] = dom
        _vm.destroy_vm(client, "tr-vm-x-web")
        assert dom.ops == ["destroy", "undefine"]
        assert dom.undefine_flags == 1 | 2 | 16 | 4  # snapshots/checkpoints/nvram cleared

    def test_destroy_absent_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client: Any = FakeClient()
        _vm.destroy_vm(client, "ghost")  # no raise

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


class _FakeStream:
    """Scripts ``virStream.recv``'s verified semantics: ``bytes`` is data
    (``b""`` = EOF), the int ``-2`` is the NONBLOCK would-block sentinel, and an
    exception instance is raised (a libvirtError from a powered-off domain)."""

    def __init__(self, script: list[Any], *, finish_raises: bool = False) -> None:
        self._script = list(script)
        self._finish_raises = finish_raises
        self.finished = False
        self.aborted = False

    def recv(self, nbytes: int) -> Any:
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def finish(self) -> None:
        if self._finish_raises:
            raise _FakeLibvirtError(1)
        self.finished = True

    def abort(self) -> None:
        self.aborted = True


class _FakeConsoleDomain:
    """``openConsole`` raises libvirtError ``fail_opens`` times, then binds."""

    def __init__(self, fail_opens: int = 0) -> None:
        self.fail_opens = fail_opens
        self.opened: list[tuple[Any, Any, int]] = []

    def openConsole(self, dev_name: Any, st: Any, flags: int) -> None:
        if self.fail_opens > 0:
            self.fail_opens -= 1
            raise _FakeLibvirtError(55)  # domain is not running (yet)
        self.opened.append((dev_name, st, flags))


class _FakeSinkClient:
    """Duck-typed LibvirtClient for the sink: lookup_domain + raw.newStream."""

    def __init__(self, dom: _FakeConsoleDomain | None, stream: _FakeStream) -> None:
        self._dom = dom
        self._stream = stream
        self.new_stream_flags: list[int] = []

    @property
    def raw(self) -> _FakeSinkClient:
        return self

    def newStream(self, flags: int) -> _FakeStream:
        self.new_stream_flags.append(flags)
        return self._stream

    def lookup_domain(self, name: str) -> _FakeConsoleDomain | None:
        return self._dom


class _FakeTime:
    """Deterministic clock: ``sleep`` advances ``monotonic``, so the sink's
    heartbeat pacing and the 60s openConsole deadline run instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def sink_time(monkeypatch: pytest.MonkeyPatch) -> _FakeTime:
    monkeypatch.setattr(
        _serial,
        "_import_libvirt",
        lambda: SimpleNamespace(VIR_STREAM_NONBLOCK=1, libvirtError=_FakeLibvirtError),
    )
    ft = _FakeTime()
    monkeypatch.setattr(_serial, "time", ft)
    return ft


class TestSerialSink:
    """The virDomainOpenConsole live-tail (BACKEND-5) against the ABC contract."""

    def test_data_would_block_heartbeat_then_eof(self, sink_time: _FakeTime) -> None:
        # recv: data -> yield; -2 (would-block) -> b"" heartbeat + paced sleep;
        # b"" (EOF: the build VM powered off) -> iteration ends.
        stream = _FakeStream([b"boot...", -2, b"TESTRANGE-RESULT: ok\n", b""])
        client: Any = _FakeSinkClient(_FakeConsoleDomain(), stream)
        got = list(_serial.read_build_result_sink(client, "tr-vm-x-web"))
        assert got == [b"boot...", b"", b"TESTRANGE-RESULT: ok\n"]
        # The heartbeat slept (the ABC contract puts pacing on the sink — an
        # unslept would-block loop would busy-spin the orchestrator).
        assert sink_time.sleeps == [1.0]
        assert stream.finished
        # The stream was opened non-blocking — recv's -2 sentinel requires it.
        assert client.new_stream_flags == [1]

    def test_libvirt_error_mid_read_is_normal_end(self, sink_time: _FakeTime) -> None:
        # A powered-off domain / aborted stream raises from recv: that is the
        # normal end of a build, not a failure — iteration just ends.
        stream = _FakeStream([b"chunk", _FakeLibvirtError(1)])
        client: Any = _FakeSinkClient(_FakeConsoleDomain(), stream)
        assert list(_serial.read_build_result_sink(client, "x")) == [b"chunk"]

    def test_open_console_retries_with_heartbeats_then_succeeds(self, sink_time: _FakeTime) -> None:
        # ORCH-15: while the domain isn't running yet, openConsole fails; the
        # sink heartbeats between attempts (a fresh stream each try) so the
        # orchestrator's deadline keeps ticking, then tails normally.
        stream = _FakeStream([b"data", b""])
        dom = _FakeConsoleDomain(fail_opens=2)
        client: Any = _FakeSinkClient(dom, stream)
        got = list(_serial.read_build_result_sink(client, "x"))
        assert got == [b"", b"", b"data"]
        assert len(client.new_stream_flags) == 3  # one stream per open attempt
        assert len(dom.opened) == 1

    def test_open_console_timeout_raises_driver_error(self, sink_time: _FakeTime) -> None:
        # A console that never opens exhausts the 60s budget: heartbeats while
        # waiting, then a typed DriverError (not an exhausted generator, which
        # the orchestrator would misread as "powered off without a result").
        stream = _FakeStream([])
        client: Any = _FakeSinkClient(_FakeConsoleDomain(fail_opens=10_000), stream)
        gen = _serial.read_build_result_sink(client, "x")
        assert next(gen) == b""
        with pytest.raises(DriverError, match="did not open"):
            list(gen)

    def test_missing_domain_raises(self, sink_time: _FakeTime) -> None:
        client: Any = _FakeSinkClient(None, _FakeStream([]))
        with pytest.raises(DriverError, match="no libvirt domain"):
            next(_serial.read_build_result_sink(client, "ghost"))

    def test_finishes_stream_on_early_break(self, sink_time: _FakeTime) -> None:
        # The orchestrator breaks out as soon as it has a result; closing()
        # must release the stream via the generator's finally.
        stream = _FakeStream([b"TESTRANGE-RESULT: ok\n", b""])
        client: Any = _FakeSinkClient(_FakeConsoleDomain(), stream)
        with closing(_serial.read_build_result_sink(client, "x")) as gen:
            next(gen)  # one chunk, then break early
        assert stream.finished

    def test_finish_failure_falls_back_to_abort(self, sink_time: _FakeTime) -> None:
        # finish() fails on a stream that ended in an error state; the release
        # falls back to abort() and never masks the build verdict.
        stream = _FakeStream([b""], finish_raises=True)
        client: Any = _FakeSinkClient(_FakeConsoleDomain(), stream)
        assert list(_serial.read_build_result_sink(client, "x")) == []
        assert stream.aborted


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

    def test_memory_snapshot_on_shutoff_vm_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ABC contract: mem=True requires a running VM (no RAM to capture off).
        monkeypatch.setattr(_vm, "_import_libvirt", _fake_libvirt)
        client = FakeClient()
        client.domains["vm"] = FakeDomain("vm", state=5)  # shut off
        with pytest.raises(DriverError, match="to be running"):
            _vm.create_snapshot(client, "vm", "s1", mem=True)  # type: ignore[arg-type]

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
