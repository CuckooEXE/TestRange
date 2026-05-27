"""Unit tests for LibvirtDriver — naming/MAC/preflight/XML rendering.

Connection + live libvirt calls are exercised in tests/integration/.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import (
    LibvirtDriver,
    LibvirtHypervisor,
    _LibvirtGuestAgent,
    _render_domain_xml,
    _render_network_xml,
    _render_pool_xml,
)
from testrange.exceptions import GuestAgentError
from testrange.networks import Network, Switch
from testrange.networks._addressing_consts import SIDECAR_CACHE_NAME
from testrange.orchestrator.runtime import _install_switch
from testrange.vms import VMRecipe, VMSpec

_INSTALL_SWITCH = _install_switch("lo")


def _seed_cache(cache: LocalCache, tmp_path: Path) -> None:
    """Populate fake debian-13 + testrange-sidecar entries used by preflight."""
    base = tmp_path / "fake.qcow2"
    base.write_bytes(b"x")
    cache.add(base, name="debian-13")
    sidecar = tmp_path / "fake-sidecar.qcow2"
    sidecar.write_bytes(b"y")
    cache.add(sidecar, name=SIDECAR_CACHE_NAME)


def _basic_recipe() -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name="web",
            devices=[CPU(1), Memory(512), OSDrive("pool1", 8), LibvirtNetworkIface("netA")],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
        ),
        communicator=SSHCommunicator("u"),
    )


def _plan() -> Plan:
    return Plan(
        LibvirtHypervisor(
            connection="qemu:///session",
            install_uplink="lo",
            networks=[
                Switch(
                    "sw1",
                    Network("netA"),
                    Network("netB"),
                    cidr="10.0.1.0/24",
                    dhcp=True,
                ),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[_basic_recipe()],
        )
    )


class TestComposeName:
    def test_deterministic(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        a = d.compose_resource_name("r1", "network", "netA")
        b = d.compose_resource_name("r1", "network", "netA")
        assert a == b
        assert a.startswith("tr_network_")
        assert a.endswith("_netA")

    def test_runid_changes_name(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        assert d.compose_resource_name("r1", "network", "netA") != d.compose_resource_name(
            "r2", "network", "netA"
        )

    def test_safe_chars(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        # libvirt name regex: [A-Za-z0-9_.+-]+
        for name in ("simple", "with-dash", "name.dot", "weird!name"):
            n = d.compose_resource_name("r1", "vm", name)
            assert re.match(r"^[A-Za-z0-9_.+\-]+$", n), n


class TestComposeMac:
    def test_deterministic(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        m1 = d.compose_mac("hello", "web", 0)
        m2 = d.compose_mac("hello", "web", 0)
        assert m1 == m2

    def test_format(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        m = d.compose_mac("hello", "web", 0)
        assert m.startswith("52:54:00:")
        assert re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", m)

    def test_different_inputs_different_macs(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        macs = {
            d.compose_mac("hello", "web", 0),
            d.compose_mac("hello", "web", 1),
            d.compose_mac("hello", "db", 0),
            d.compose_mac("other", "web", 0),
        }
        assert len(macs) == 4


class TestPreflight:
    def test_clean_creates_pool_root(self, tmp_path: Path) -> None:
        # Clean preflight must (a) report no errors and (b) leave pool_root
        # on disk for /session URIs (system URIs let libvirtd build it).
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path / "pools")
        cache = LocalCache(root=tmp_path / "c")
        _seed_cache(cache, tmp_path)
        mgr = CacheManager(local=cache)
        report = d.preflight(_plan(), cache_manager=mgr, install_switch=_INSTALL_SWITCH)
        assert bool(report), report.render()
        assert report.errors == ()
        assert (tmp_path / "pools").exists()

    def test_cache_miss(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        mgr = CacheManager(local=LocalCache(root=tmp_path / "c"))
        report = d.preflight(_plan(), cache_manager=mgr, install_switch=_INSTALL_SWITCH)
        codes = {f.code for f in report.errors}
        assert "cache_miss" in codes

    def test_subnet_overlap(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        cache = LocalCache(root=tmp_path / "c")
        _seed_cache(cache, tmp_path)
        mgr = CacheManager(local=cache)
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///session",
                install_uplink="lo",
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.0.0.0/24", dhcp=True),
                    Switch("sw2", Network("netB"), cidr="10.0.0.128/25", dhcp=True),
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe()],
            )
        )
        report = d.preflight(plan, cache_manager=mgr, install_switch=_INSTALL_SWITCH)
        codes = {f.code for f in report.errors}
        assert "subnet_overlap" in codes

    def test_install_cidr_overlap_caught(self, tmp_path: Path) -> None:
        # A user network that collides with the transient install CIDR must
        # be caught at preflight (not at install-time when libvirt would
        # fail with an opaque error).
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        cache = LocalCache(root=tmp_path / "c")
        _seed_cache(cache, tmp_path)
        mgr = CacheManager(local=cache)
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///session",
                install_uplink="lo",
                networks=[
                    Switch("sw1", Network("netA"), cidr="10.97.99.0/25", dhcp=True),
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe()],
            )
        )
        report = d.preflight(plan, cache_manager=mgr, install_switch=_INSTALL_SWITCH)
        overlap_findings = [f for f in report.errors if f.code == "subnet_overlap"]
        assert overlap_findings, report.render()
        assert any(f.fix_hint and "install switch" in f.fix_hint for f in overlap_findings), [
            f.fix_hint for f in overlap_findings
        ]

    def test_system_uri_skips_user_side_pool_root_mkdir(self, tmp_path: Path) -> None:
        # Even when pool_root points at an unwritable location, system-mode
        # preflight should not error — libvirt builds the dir at pool create time.
        unwritable = Path("/nonexistent-root/cannot-mkdir/here")
        d = LibvirtDriver(uri="qemu:///system", pool_root=unwritable)
        cache = LocalCache(root=tmp_path / "c")
        _seed_cache(cache, tmp_path)
        mgr = CacheManager(local=cache)
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///system",
                install_uplink="lo",
                networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24", dhcp=True)],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe()],
            )
        )
        report = d.preflight(plan, cache_manager=mgr, install_switch=_INSTALL_SWITCH)
        codes = {f.code for f in report.errors}
        assert "pool_root_unwritable" not in codes
        assert not unwritable.exists()


class TestXMLRendering:
    def test_bare_switch_xml(self) -> None:
        # No infra flags: libvirt manages a passive bridge, no <ip>.
        n = Network("netA")
        sw = Switch("sw1", n, cidr="10.0.1.0/24")
        xml = _render_network_xml(n, sw, "tr_net_abc_netA")
        assert "<name>tr_net_abc_netA</name>" in xml
        assert "<forward" not in xml
        assert "<dhcp>" not in xml
        assert "<ip" not in xml

    def test_uplink_no_nat_xml_uses_bridge_mode(self) -> None:
        # uplink without nat: switch bridge IS the uplink bridge.
        n = Network("netA")
        sw = Switch("sw1", n, cidr="10.0.1.0/24", uplink="eth0")
        xml = _render_network_xml(n, sw, "net_abc", bridge_name="tr-bridge0")
        assert "<forward mode='bridge'/>" in xml
        assert "<bridge name='tr-bridge0'/>" in xml

    def test_nat_uplink_xml_references_isolated_bridge(self) -> None:
        # nat+uplink: switch stays isolated; orchestrator passes the
        # testrange-created isolated switch-bridge name.
        n = Network("netA")
        sw = Switch("sw1", n, cidr="10.0.1.0/24", uplink="eth0", nat=True)
        xml = _render_network_xml(n, sw, "net_abc", bridge_name="tr-iso0")
        assert "<forward mode='bridge'/>" in xml
        assert "<bridge name='tr-iso0'/>" in xml

    def test_mgmt_only_xml_requires_bridge_name(self) -> None:
        # mgmt without uplink: orchestrator creates an isolated bridge
        # for mgmt-IP assignment; renderer requires the bridge_name.
        n = Network("netA")
        sw = Switch("sw1", n, cidr="10.0.1.0/24", mgmt=True)
        xml = _render_network_xml(n, sw, "net_abc", bridge_name="tr-mgmt0")
        assert "<bridge name='tr-mgmt0'/>" in xml

    def test_pool_xml(self, tmp_path: Path) -> None:
        xml = _render_pool_xml("tr_pool_abc_pool1", tmp_path / "p")
        assert "<pool type='dir'>" in xml
        assert "<name>tr_pool_abc_pool1</name>" in xml
        assert str(tmp_path / "p") in xml


class TestDriverDispatch:
    def test_destroy_dispatch_for_unknown_kind(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        with pytest.raises(NotImplementedError):
            d.destroy("unknown", "x")

    def test_conn_property_unconnected(self, tmp_path: Path) -> None:
        from testrange.exceptions import DriverError

        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        with pytest.raises(DriverError):
            _ = d.conn


class TestPoolRootDefault:
    def test_session_uri_defaults_to_user_path(self) -> None:
        d = LibvirtDriver(uri="qemu:///session")
        assert d.pool_root == Path.home() / ".local" / "share" / "testrange" / "pools"

    def test_system_uri_defaults_to_var_lib(self) -> None:
        d = LibvirtDriver(uri="qemu:///system")
        assert d.pool_root == Path("/var/lib/libvirt/images/testrange")

    def test_remote_system_uri_defaults_to_var_lib(self) -> None:
        d = LibvirtDriver(uri="qemu+ssh://root@host/system")
        assert d.pool_root == Path("/var/lib/libvirt/images/testrange")

    def test_explicit_pool_root_wins(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "p")
        assert d.pool_root == tmp_path / "p"


class _FakeStorageVol:
    def __init__(self, name: str, *, contents: bytes = b"") -> None:
        self.name = name
        self.deleted = False
        self.upload_called_with: tuple[Any, int, int, int] | None = None
        self.download_called_with: tuple[Any, int, int, int] | None = None
        self.contents = contents

    def upload(self, stream: Any, offset: int, length: int, flags: int) -> None:
        self.upload_called_with = (stream, offset, length, flags)

    def download(self, stream: _FakeStream, offset: int, length: int, flags: int) -> None:
        self.download_called_with = (stream, offset, length, flags)
        # Seed the stream with the volume's bytes so recvAll has data to deliver.
        stream.to_deliver = self.contents

    def info(self) -> list[int]:
        # libvirt returns [type, capacity, allocation]; we only use capacity.
        return [0, len(self.contents), len(self.contents)]

    def delete(self, flags: int) -> None:
        self.deleted = True


class _FakeStream:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.to_deliver = b""
        self.finished = False
        self.aborted = False

    def sendAll(self, handler: Any, opaque: Any) -> None:
        # Mirror libvirt's contract: pump bytes from handler until empty.
        # Pass -1 (Python's "read everything") so the handler returns whatever
        # remains in one shot.
        while True:
            chunk = handler(self, -1, opaque)
            if not chunk:
                return
            self.sent.extend(chunk)

    def recvAll(self, handler: Any, opaque: Any) -> None:
        # Deliver to_deliver bytes to the handler in a single shot.
        if self.to_deliver:
            handler(self, self.to_deliver, opaque)

    def finish(self) -> None:
        self.finished = True

    def abort(self) -> None:
        self.aborted = True


class _FakePool:
    def __init__(self) -> None:
        self.volumes: dict[str, _FakeStorageVol] = {}
        self.refresh_calls = 0
        self.create_xmls: list[str] = []
        self.autostart: bool | None = None
        self.built = False
        self.started = False
        self.active = True
        self.deleted = False
        self.undefined = False

    def isActive(self) -> bool:
        return self.active

    def destroy(self) -> None:
        self.active = False

    def delete(self, flags: int) -> None:
        self.deleted = True

    def undefine(self) -> None:
        self.undefined = True

    def storageVolLookupByName(self, name: str) -> _FakeStorageVol:
        import libvirt as _libvirt  # real module for libvirtError

        if name not in self.volumes:
            raise _libvirt.libvirtError(f"no volume {name}")
        return self.volumes[name]

    def listVolumes(self) -> list[str]:
        return [v.name for v in self.volumes.values() if not v.deleted]

    def createXML(self, xml: str, flags: int) -> _FakeStorageVol:
        self.create_xmls.append(xml)
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)  # type: ignore[union-attr]
        v = _FakeStorageVol(name)
        self.volumes[name] = v
        return v

    def createXMLFrom(self, xml: str, source_vol: _FakeStorageVol, flags: int) -> _FakeStorageVol:
        del flags
        self.create_xmls.append(xml)
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)  # type: ignore[union-attr]
        # libvirt's dir-pool clone reads through the source's backing chain
        # via qemu-img convert. Our fake just copies the source's bytes.
        v = _FakeStorageVol(name, contents=source_vol.contents)
        self.volumes[name] = v
        return v

    def refresh(self, flags: int) -> None:
        self.refresh_calls += 1

    def setAutostart(self, flag: bool) -> None:
        self.autostart = flag

    def build(self, flags: int) -> None:
        self.built = True

    def create(self) -> None:
        self.started = True


class _FakeSnapshot:
    def __init__(self, name: str, parent: _FakeDomain) -> None:
        self.name = name
        self._parent = parent
        self.deleted = False

    def delete(self, flags: int) -> None:
        self.deleted = True
        self._parent._snapshots = [s for s in self._parent._snapshots if s.name != self.name]


class _FakeDomain:
    def __init__(self, name: str) -> None:
        self.name = name
        self._snapshots: list[_FakeSnapshot] = []
        self.snapshot_xmls: list[tuple[str, int]] = []  # (xml, flags)
        self.reverted_to: _FakeSnapshot | None = None
        self._active = True
        self.undefined = False

    def snapshotCreateXML(self, xml: str, flags: int) -> _FakeSnapshot:
        self.snapshot_xmls.append((xml, flags))
        name_match = re.search(r"<name>([^<]+)</name>", xml)
        assert name_match is not None
        snap = _FakeSnapshot(name_match.group(1), self)
        self._snapshots.append(snap)
        return snap

    def snapshotListNames(self) -> list[str]:
        return [s.name for s in self._snapshots]

    def snapshotLookupByName(self, name: str) -> _FakeSnapshot:
        import libvirt as _libvirt

        for s in self._snapshots:
            if s.name == name:
                return s
        raise _libvirt.libvirtError(f"no domain snapshot {name}")

    def revertToSnapshot(self, snap: _FakeSnapshot, flags: int) -> None:
        del flags
        self.reverted_to = snap

    def isActive(self) -> bool:
        return self._active

    def destroy(self) -> None:
        self._active = False

    def undefine(self) -> None:
        self.undefined = True


class _FakeConn:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.streams: list[_FakeStream] = []
        self.defined_pool_xmls: list[str] = []
        self.domains: dict[str, _FakeDomain] = {}

    def storagePoolLookupByName(self, name: str) -> _FakePool:
        return self.pool

    def storagePoolDefineXML(self, xml: str) -> _FakePool:
        self.defined_pool_xmls.append(xml)
        return self.pool

    def newStream(self, flags: int) -> _FakeStream:
        s = _FakeStream()
        self.streams.append(s)
        return s

    def lookupByName(self, name: str) -> _FakeDomain:
        if name not in self.domains:
            self.domains[name] = _FakeDomain(name)
        return self.domains[name]


class TestUploadToPool:
    def test_streams_source_bytes_to_new_volume(self, tmp_path: Path) -> None:
        src = tmp_path / "base.qcow2"
        payload = b"qcow2-header-bytes\x00\x01\x02" * 1024
        src.write_bytes(payload)

        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        d._conn = _FakeConn()  # type: ignore[assignment]

        target = d.compose_volume_ref("p1", "tr_base_abc.qcow2")
        out = d.upload_to_pool(target, src)

        assert out == target
        conn: _FakeConn = d._conn  # type: ignore[assignment]
        assert "tr_base_abc.qcow2" in conn.pool.volumes
        vol = conn.pool.volumes["tr_base_abc.qcow2"]
        assert vol.upload_called_with is not None
        _, offset, length, _ = vol.upload_called_with
        assert offset == 0
        assert length == len(payload)
        assert len(conn.streams) == 1
        assert bytes(conn.streams[0].sent) == payload
        assert conn.streams[0].finished is True
        assert conn.pool.refresh_calls == 1
        assert f"<capacity unit='bytes'>{len(payload)}</capacity>" in conn.pool.create_xmls[0]

    def test_idempotent_when_volume_exists(self, tmp_path: Path) -> None:
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"x" * 100)

        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.pool.volumes["already-here.qcow2"] = _FakeStorageVol("already-here.qcow2")
        d._conn = conn  # type: ignore[assignment]

        target = d.compose_volume_ref("p1", "already-here.qcow2")
        out = d.upload_to_pool(target, src)
        assert out == target
        assert conn.streams == []
        assert conn.pool.create_xmls == []

    def test_failure_deletes_partial_volume(self, tmp_path: Path) -> None:
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"x" * 100)

        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        d._conn = conn  # type: ignore[assignment]

        original_send_all = _FakeStream.sendAll

        def patched_send_all(_self: _FakeStream, _handler: Any, _opaque: Any) -> None:
            raise RuntimeError("simulated network failure")

        _FakeStream.sendAll = patched_send_all  # type: ignore[method-assign]
        target = d.compose_volume_ref("p1", "broken.qcow2")
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                d.upload_to_pool(target, src)
        finally:
            _FakeStream.sendAll = original_send_all  # type: ignore[method-assign]

        assert conn.pool.volumes["broken.qcow2"].deleted is True
        assert conn.streams[0].aborted is True


class TestCreatePool:
    def test_does_not_mkdir_from_python(self, tmp_path: Path) -> None:
        # In system mode the Python process can't write under /var/lib/libvirt;
        # the per-pool target dir must be built by libvirt itself via sp.build().
        unwritable_root = Path("/nonexistent-root/should-not-be-created")
        d = LibvirtDriver(uri="qemu:///system", pool_root=unwritable_root)
        conn = _FakeConn()
        d._conn = conn  # type: ignore[assignment]

        pool = StoragePool("pool1", 32)
        d.create_pool(pool, "tr_pool_abc_pool1")

        assert not unwritable_root.exists()
        # libvirt is the one creating + starting the pool
        assert len(conn.defined_pool_xmls) == 1
        assert "<name>tr_pool_abc_pool1</name>" in conn.defined_pool_xmls[0]
        assert conn.pool.autostart is True
        assert conn.pool.built is True
        assert conn.pool.started is True

    def test_pool_xml_target_path_is_under_pool_root(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "p")
        conn = _FakeConn()
        d._conn = conn  # type: ignore[assignment]
        d.create_pool(StoragePool("pool1", 32), "tr_pool_xyz_pool1")
        xml = conn.defined_pool_xmls[0]
        assert str(tmp_path / "p" / "tr_pool_xyz_pool1") in xml


class TestDestroyPool:
    def test_destroys_deletes_then_undefines(self, tmp_path: Path) -> None:
        # destroy_pool must call destroy (stop) → delete (rmdir) → undefine.
        # Skipping the delete step leaks an empty per-run directory under
        # /var/lib/libvirt/images/testrange/ on each teardown.
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        d._conn = conn  # type: ignore[assignment]

        d.destroy_pool("tr_pool_xyz_pool1")

        assert conn.pool.active is False  # destroy() stopped it
        assert conn.pool.deleted is True  # delete(0) removed the on-disk dir
        assert conn.pool.undefined is True  # undefine() removed the libvirt def


class TestWriteToPool:
    def test_streams_bytes_as_raw_volume(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        d._conn = conn  # type: ignore[assignment]

        data = b"cidata-iso-bytes\x00\x01\x02" * 256
        target = d.compose_volume_ref("p1", "seed.iso")
        out = d.write_to_pool(target, data)

        assert out == target
        assert "seed.iso" in conn.pool.volumes
        assert len(conn.streams) == 1
        assert bytes(conn.streams[0].sent) == data
        assert conn.streams[0].finished is True
        assert conn.pool.refresh_calls == 1
        xml = conn.pool.create_xmls[0]
        assert "<format type='raw'/>" in xml
        assert f"<capacity unit='bytes'>{len(data)}</capacity>" in xml

    def test_replaces_existing_volume(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        old = _FakeStorageVol("seed.iso")
        conn.pool.volumes["seed.iso"] = old
        d._conn = conn  # type: ignore[assignment]

        d.write_to_pool(d.compose_volume_ref("p1", "seed.iso"), b"fresh-bytes")

        # Old vol was deleted; a new one created with the fresh data.
        assert old.deleted is True
        assert conn.pool.volumes["seed.iso"] is not old
        assert bytes(conn.streams[0].sent) == b"fresh-bytes"


class TestDownloadFromPool:
    def test_streams_volume_bytes_to_dest(self, tmp_path: Path) -> None:
        payload = b"post-install-disk-bytes\x00\xff" * 1024
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.pool.volumes["disk.qcow2"] = _FakeStorageVol("disk.qcow2", contents=payload)
        d._conn = conn  # type: ignore[assignment]

        dest = tmp_path / "out.qcow2"
        vol_ref = d.compose_volume_ref("p1", "disk.qcow2")
        out = d.download_from_pool(vol_ref, dest)

        assert out == dest
        assert dest.read_bytes() == payload
        # No intermediate clone is created — the in-pool volume is streamed directly.
        vol = conn.pool.volumes["disk.qcow2"]
        assert vol.download_called_with is not None
        _, offset, length, _ = vol.download_called_with
        assert offset == 0
        assert length == 0  # libvirt's "stream until EOF" sentinel
        assert conn.streams[0].finished is True

    def test_failure_unlinks_partial_dest(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.pool.volumes["disk.qcow2"] = _FakeStorageVol("disk.qcow2", contents=b"x" * 100)
        d._conn = conn  # type: ignore[assignment]

        original_recv = _FakeStream.recvAll

        def patched(_self: _FakeStream, _handler: Any, _opaque: Any) -> None:
            raise RuntimeError("simulated read failure")

        _FakeStream.recvAll = patched  # type: ignore[method-assign]
        dest = tmp_path / "out.qcow2"
        vol_ref = d.compose_volume_ref("p1", "disk.qcow2")
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                d.download_from_pool(vol_ref, dest)
        finally:
            _FakeStream.recvAll = original_recv  # type: ignore[method-assign]

        assert not dest.exists()
        assert conn.streams[0].aborted is True


class TestCreateDiskFromBase:
    def test_full_copy_via_createxmlfrom(self, tmp_path: Path) -> None:
        # The in-pool source vol with a known capacity; the new disk should
        # be created via createXMLFrom (full copy, no backingStore) with that
        # capacity inherited.
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        source = _FakeStorageVol("base.qcow2", contents=b"BASE-BYTES" * 1024)
        conn.pool.volumes["base.qcow2"] = source
        d._conn = conn  # type: ignore[assignment]

        source_ref = d.compose_volume_ref("p1", "base.qcow2")
        target_ref = d.compose_volume_ref("p1", "web.qcow2")
        out = d.create_disk_from_base(target_ref, source_ref)

        assert out == target_ref
        # New vol exists and was cloned from the source (fake clone copies contents).
        assert "web.qcow2" in conn.pool.volumes
        assert conn.pool.volumes["web.qcow2"].contents == source.contents
        # XML used has no <backingStore> and capacity matches source.
        clone_xmls = [x for x in conn.pool.create_xmls if "web.qcow2" in x]
        assert clone_xmls, "expected createXMLFrom call"
        assert "<backingStore>" not in clone_xmls[0]
        assert f"<capacity unit='bytes'>{len(source.contents)}</capacity>" in clone_xmls[0]


class TestSnapshots:
    def _driver_with_vm(self, tmp_path: Path, vm: str = "tr_vm_abc_web") -> LibvirtDriver:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.domains[vm] = _FakeDomain(vm)
        d._conn = conn  # type: ignore[assignment]
        return d

    def test_create_disk_only_passes_disk_only_flag(self, tmp_path: Path) -> None:
        import libvirt as _libvirt

        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "pre-test", "before nginx", mem=False)
        dom = d._conn.domains["tr_vm_abc_web"]  # type: ignore[union-attr]
        xml, flags = dom.snapshot_xmls[0]
        assert "<name>pre-test</name>" in xml
        assert "<description>before nginx</description>" in xml
        assert flags == _libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY

    def test_create_with_mem_passes_default_flags(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "pre-test", mem=True)
        dom = d._conn.domains["tr_vm_abc_web"]  # type: ignore[union-attr]
        _, flags = dom.snapshot_xmls[0]
        assert flags == 0  # no DISK_ONLY → memory included when running

    def test_create_without_description_omits_description_element(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "just-a-name")
        dom = d._conn.domains["tr_vm_abc_web"]  # type: ignore[union-attr]
        xml, _ = dom.snapshot_xmls[0]
        assert "<description>" not in xml

    def test_list_returns_names_oldest_first(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "first")
        d.create_snapshot("tr_vm_abc_web", "second")
        d.create_snapshot("tr_vm_abc_web", "third")
        assert d.list_snapshots("tr_vm_abc_web") == ["first", "second", "third"]

    def test_delete_removes_named_snapshot(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "keep")
        d.create_snapshot("tr_vm_abc_web", "drop")
        d.delete_snapshot("tr_vm_abc_web", "drop")
        assert d.list_snapshots("tr_vm_abc_web") == ["keep"]

    def test_delete_missing_is_a_noop(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        # No snapshots; deleting one that doesn't exist must not raise.
        d.delete_snapshot("tr_vm_abc_web", "never-existed")
        assert d.list_snapshots("tr_vm_abc_web") == []

    def test_create_duplicate_name_raises(self, tmp_path: Path) -> None:
        from testrange.exceptions import DriverError

        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "snap")
        with pytest.raises(DriverError, match="already exists"):
            d.create_snapshot("tr_vm_abc_web", "snap")
        # The first snapshot is still there; no duplicate added.
        assert d.list_snapshots("tr_vm_abc_web") == ["snap"]

    def test_restore_reverts_to_named_snapshot(self, tmp_path: Path) -> None:
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "pre-test")
        d.restore_snapshot("tr_vm_abc_web", "pre-test")
        # The fake domain records the revert; verify it happened.
        dom = d._conn.domains["tr_vm_abc_web"]  # type: ignore[union-attr]
        assert dom.reverted_to is not None
        assert dom.reverted_to.name == "pre-test"

    def test_restore_missing_raises(self, tmp_path: Path) -> None:
        from testrange.exceptions import DriverError

        d = self._driver_with_vm(tmp_path)
        with pytest.raises(DriverError, match="not found"):
            d.restore_snapshot("tr_vm_abc_web", "never-existed")

    def test_destroy_vm_clears_snapshots_first(self, tmp_path: Path) -> None:
        # libvirt won't undefine a domain that still has snapshots — the
        # driver must clean them up before undefine. (Without this, the
        # smoke run's snapshot_lifecycle test left a snapshot behind that
        # blocked teardown.)
        d = self._driver_with_vm(tmp_path)
        d.create_snapshot("tr_vm_abc_web", "lingering")
        assert d.list_snapshots("tr_vm_abc_web") == ["lingering"]
        d.destroy_vm("tr_vm_abc_web")
        assert d.list_snapshots("tr_vm_abc_web") == []


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _FakeQGA:
    """Stand-in for the libvirt_qemu module.

    ``qemuAgentCommand`` parses the JSON wire command, records it, and
    returns the next scripted response for that command: a dict is
    JSON-encoded and returned, a ``BaseException`` is raised. A command's
    script list is consumed front-to-back until one entry remains, which
    then sticks (so polled commands can be scripted as a short list).
    """

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def qemuAgentCommand(self, dom: Any, wire: str, timeout: int, flags: int) -> str:
        del dom, timeout, flags
        payload = json.loads(wire)
        self.calls.append(payload)
        queue = self._script[payload["execute"]]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)


def _agent_with(
    monkeypatch: pytest.MonkeyPatch, script: dict[str, list[Any]]
) -> tuple[_LibvirtGuestAgent, _FakeQGA]:
    d = LibvirtDriver(uri="qemu:///system", pool_root=Path("/tmp"))
    d._conn = _FakeConn()  # type: ignore[assignment]
    fake = _FakeQGA(script)
    monkeypatch.setattr("testrange.drivers.libvirt._import_libvirt_qemu", lambda: fake)
    monkeypatch.setattr("testrange.drivers.libvirt.time.sleep", lambda _s: None)
    return _LibvirtGuestAgent(d, "tr_vm_abc_web"), fake


class TestLibvirtGuestAgent:
    def test_execute_runs_and_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, fake = _agent_with(
            monkeypatch,
            {
                "guest-exec": [{"return": {"pid": 7}}],
                "guest-exec-status": [
                    {"return": {"exited": False}},
                    {
                        "return": {
                            "exited": True,
                            "exitcode": 0,
                            "out-data": _b64(b"hi\n"),
                            "err-data": _b64(b""),
                        }
                    },
                ],
            },
        )
        r = agent.execute(["echo", "hi"])
        assert r.exit_code == 0
        assert r.stdout == b"hi\n"
        assert r.stderr == b""
        exec_args = fake.calls[0]["arguments"]
        assert fake.calls[0]["execute"] == "guest-exec"
        assert exec_args["path"] == "echo"
        assert exec_args["arg"] == ["hi"]
        assert exec_args["capture-output"] is True

    def test_execute_cwd_shim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, fake = _agent_with(
            monkeypatch,
            {
                "guest-exec": [{"return": {"pid": 1}}],
                "guest-exec-status": [
                    {"return": {"exited": True, "exitcode": 0, "out-data": "", "err-data": ""}}
                ],
            },
        )
        agent.execute(["ls", "-la"], cwd="/var/log")
        args = fake.calls[0]["arguments"]
        assert args["path"] == "sh"
        assert args["arg"][0] == "-c"
        assert "cd -- /var/log" in args["arg"][1]
        assert "exec ls -la" in args["arg"][1]

    def test_execute_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, _ = _agent_with(
            monkeypatch,
            {
                "guest-exec": [{"return": {"pid": 2}}],
                "guest-exec-status": [
                    {
                        "return": {
                            "exited": True,
                            "exitcode": 3,
                            "out-data": "",
                            "err-data": _b64(b"boom"),
                        }
                    }
                ],
            },
        )
        r = agent.execute(["false"])
        assert r.exit_code == 3
        assert r.stderr == b"boom"

    def test_qga_error_response_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, _ = _agent_with(
            monkeypatch,
            {"guest-exec": [{"error": {"class": "GenericError", "desc": "boom"}}]},
        )
        with pytest.raises(GuestAgentError, match="guest-exec"):
            agent.execute(["echo", "hi"])

    def test_libvirt_error_wraps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import libvirt

        agent, _ = _agent_with(
            monkeypatch,
            {"guest-exec": [libvirt.libvirtError("kaboom")]},
        )
        with pytest.raises(GuestAgentError, match="failed"):
            agent.execute(["echo", "hi"])

    def test_agent_not_ready_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import libvirt

        agent, fake = _agent_with(
            monkeypatch,
            {
                "guest-exec": [
                    libvirt.libvirtError("Guest agent is not responding"),
                    libvirt.libvirtError("Guest agent is not responding"),
                    {"return": {"pid": 9}},
                ],
                "guest-exec-status": [
                    {"return": {"exited": True, "exitcode": 0, "out-data": "", "err-data": ""}}
                ],
            },
        )
        r = agent.execute(["echo", "hi"])
        assert r.exit_code == 0
        # guest-exec retried twice before succeeding.
        assert sum(1 for c in fake.calls if c["execute"] == "guest-exec") == 3

    def test_read_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, fake = _agent_with(
            monkeypatch,
            {
                "guest-file-open": [{"return": 9}],
                "guest-file-read": [
                    {"return": {"buf-b64": _b64(b"chunk1"), "eof": False}},
                    {"return": {"buf-b64": _b64(b"chunk2"), "eof": True}},
                ],
                "guest-file-close": [{"return": {}}],
            },
        )
        assert agent.read_file("/etc/hostname") == b"chunk1chunk2"
        assert any(c["execute"] == "guest-file-close" for c in fake.calls)

    def test_write_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, fake = _agent_with(
            monkeypatch,
            {
                "guest-file-open": [{"return": 4}],
                "guest-file-write": [{"return": {"count": 5}}],
                "guest-file-close": [{"return": {}}],
            },
        )
        agent.write_file("/tmp/x", b"hello")
        write_call = next(c for c in fake.calls if c["execute"] == "guest-file-write")
        assert write_call["arguments"]["buf-b64"] == _b64(b"hello")
        assert write_call["arguments"]["count"] == 5

    def test_domain_xml_renders_qga_channel(self) -> None:
        xml = _render_domain_xml(
            "tr_vm_abc_web",
            _basic_recipe().spec,
            os_disk_path=Path("/var/lib/libvirt/images/x.qcow2"),
            seed_iso_path=None,
            network_refs={"netA": "tr_network_abc_netA"},
            macs=["52:54:00:00:00:01"],
        )
        assert "<channel type='unix'>" in xml
        assert "org.qemu.guest_agent.0" in xml
