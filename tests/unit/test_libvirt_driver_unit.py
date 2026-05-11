"""Unit tests for LibvirtDriver — naming/MAC/preflight/XML rendering.

Connection + live libvirt calls are exercised in tests/integration/.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, LibvirtNetworkIface, Memory, OSDrive, StoragePool
from testrange.drivers.libvirt import (
    LibvirtDriver,
    LibvirtHypervisor,
    _render_network_xml,
    _render_pool_xml,
)
from testrange.networks import Network, Switch
from testrange.vms import VMRecipe, VMSpec


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
            networks=[
                Switch(
                    "sw1",
                    Network("netA", "10.0.1.0/24"),
                    Network("netB", "10.0.2.0/24"),
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
    def test_clean(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        # Cache must resolve for clean preflight:
        cache = LocalCache(root=tmp_path / "c")
        src = tmp_path / "fake.qcow2"
        src.write_bytes(b"x")
        cache.add(src, name="debian-13")
        mgr = CacheManager(local=cache)
        report = d.preflight(_plan(), cache_manager=mgr)
        assert bool(report), report.render()
        assert report.errors == ()

    def test_cache_miss(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        mgr = CacheManager(local=LocalCache(root=tmp_path / "c"))
        report = d.preflight(_plan(), cache_manager=mgr)
        codes = {f.code for f in report.errors}
        assert "cache_miss" in codes

    def test_subnet_overlap(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        cache = LocalCache(root=tmp_path / "c")
        src = tmp_path / "fake.qcow2"
        src.write_bytes(b"x")
        cache.add(src, name="debian-13")
        mgr = CacheManager(local=cache)
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///session",
                networks=[
                    Switch(
                        "sw1",
                        Network("netA", "10.0.0.0/24"),
                        Network("netB", "10.0.0.128/25"),
                    ),
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe()],
            )
        )
        report = d.preflight(plan, cache_manager=mgr)
        codes = {f.code for f in report.errors}
        assert "subnet_overlap" in codes

    def test_pool_root_writable(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path / "pools")
        cache = LocalCache(root=tmp_path / "c")
        src = tmp_path / "fake.qcow2"
        src.write_bytes(b"x")
        cache.add(src, name="debian-13")
        mgr = CacheManager(local=cache)
        report = d.preflight(_plan(), cache_manager=mgr)
        assert (tmp_path / "pools").exists()
        assert bool(report)

    def test_system_uri_skips_user_side_pool_root_mkdir(self, tmp_path: Path) -> None:
        # Even when pool_root points at an unwritable location, system-mode
        # preflight should not error — libvirt builds the dir at pool create time.
        unwritable = Path("/nonexistent-root/cannot-mkdir/here")
        d = LibvirtDriver(uri="qemu:///system", pool_root=unwritable)
        cache = LocalCache(root=tmp_path / "c")
        src = tmp_path / "fake.qcow2"
        src.write_bytes(b"x")
        cache.add(src, name="debian-13")
        mgr = CacheManager(local=cache)
        plan = Plan(
            LibvirtHypervisor(
                connection="qemu:///system",
                networks=[Switch("sw1", Network("netA", "10.0.1.0/24"))],
                pools=[StoragePool("pool1", 32)],
                vms=[_basic_recipe()],
            )
        )
        report = d.preflight(plan, cache_manager=mgr)
        codes = {f.code for f in report.errors}
        assert "pool_root_unwritable" not in codes
        assert not unwritable.exists()


class TestXMLRendering:
    def test_network_xml_has_required_fields(self) -> None:
        n = Network("netA", "10.0.1.0/24", dhcp=True, dns=True)
        sw = Switch("sw1", internet=True)
        xml = _render_network_xml(n, sw, "tr_net_abc_netA")
        assert "<name>tr_net_abc_netA</name>" in xml
        assert "<forward mode='nat'/>" in xml
        assert "10.0.1.1" in xml  # gateway = first usable
        assert "255.255.255.0" in xml
        assert "<dhcp>" in xml

    def test_network_xml_air_gapped(self) -> None:
        n = Network("netA", "10.0.1.0/24")
        sw = Switch("sw1", internet=False)
        xml = _render_network_xml(n, sw, "x")
        assert "<forward" not in xml

    def test_network_xml_dns_off(self) -> None:
        n = Network("netA", "10.0.1.0/24", dns=False)
        sw = Switch("sw1")
        xml = _render_network_xml(n, sw, "x")
        assert "<domain" not in xml

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

    def download(self, stream: "_FakeStream", offset: int, length: int, flags: int) -> None:
        self.download_called_with = (stream, offset, length, flags)
        # Seed the stream with the volume's bytes so recvAll has data to deliver.
        stream.to_deliver = self.contents

    def info(self) -> list[int]:
        # libvirt returns [type, capacity, allocation]; we only use capacity.
        return [0, len(self.contents), len(self.contents)]

    def delete(self, flags: int) -> None:  # noqa: ARG002
        self.deleted = True


class _FakeStream:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.to_deliver = b""
        self.finished = False
        self.aborted = False

    def sendAll(self, handler: Any, opaque: Any) -> None:  # noqa: N802
        # Mirror libvirt's contract: pump bytes from handler until empty.
        while True:
            chunk = handler(self, 64 * 1024, opaque)
            if not chunk:
                return
            self.sent.extend(chunk)

    def recvAll(self, handler: Any, opaque: Any) -> None:  # noqa: N802
        # Deliver to_deliver bytes to the handler in 64K chunks.
        chunk_size = 64 * 1024
        view = memoryview(self.to_deliver)
        i = 0
        while i < len(view):
            chunk = bytes(view[i : i + chunk_size])
            handler(self, chunk, opaque)
            i += len(chunk)

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

    def isActive(self) -> bool:  # noqa: N802
        return self.active

    def destroy(self) -> None:
        self.active = False

    def delete(self, flags: int) -> None:  # noqa: ARG002
        self.deleted = True

    def undefine(self) -> None:
        self.undefined = True

    def storageVolLookupByName(self, name: str) -> _FakeStorageVol:  # noqa: N802
        import libvirt as _libvirt  # real module for libvirtError

        if name not in self.volumes:
            raise _libvirt.libvirtError(f"no volume {name}")
        return self.volumes[name]

    def createXML(self, xml: str, flags: int) -> _FakeStorageVol:  # noqa: N802,ARG002
        self.create_xmls.append(xml)
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)  # type: ignore[union-attr]
        v = _FakeStorageVol(name)
        self.volumes[name] = v
        return v

    def createXMLFrom(  # noqa: N802
        self, xml: str, source_vol: _FakeStorageVol, flags: int
    ) -> _FakeStorageVol:
        del flags
        self.create_xmls.append(xml)
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)  # type: ignore[union-attr]
        # libvirt's dir-pool clone reads through the source's backing chain
        # via qemu-img convert. Our fake just copies the source's bytes.
        v = _FakeStorageVol(name, contents=source_vol.contents)
        self.volumes[name] = v
        return v

    def refresh(self, flags: int) -> None:  # noqa: ARG002
        self.refresh_calls += 1

    def setAutostart(self, flag: bool) -> None:  # noqa: N802
        self.autostart = flag

    def build(self, flags: int) -> None:  # noqa: ARG002
        self.built = True

    def create(self) -> None:
        self.started = True


class _FakeConn:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.streams: list[_FakeStream] = []
        self.defined_pool_xmls: list[str] = []

    def storagePoolLookupByName(self, name: str) -> _FakePool:  # noqa: N802,ARG002
        return self.pool

    def storagePoolDefineXML(self, xml: str) -> _FakePool:  # noqa: N802
        self.defined_pool_xmls.append(xml)
        return self.pool

    def newStream(self, flags: int) -> _FakeStream:  # noqa: N802,ARG002
        s = _FakeStream()
        self.streams.append(s)
        return s


class TestUploadToPool:
    def test_streams_source_bytes_to_new_volume(self, tmp_path: Path) -> None:
        src = tmp_path / "base.qcow2"
        payload = b"qcow2-header-bytes\x00\x01\x02" * 1024
        src.write_bytes(payload)

        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        d._conn = _FakeConn()  # type: ignore[assignment]

        out = d.upload_to_pool("p1", "tr_base_abc.qcow2", src)

        assert out == tmp_path / "pools" / "p1" / "tr_base_abc.qcow2"
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

        out = d.upload_to_pool("p1", "already-here.qcow2", src)
        assert out == tmp_path / "pools" / "p1" / "already-here.qcow2"
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
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                d.upload_to_pool("p1", "broken.qcow2", src)
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
        out = d.write_to_pool("p1", "seed.iso", data)

        assert out == tmp_path / "pools" / "p1" / "seed.iso"
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

        d.write_to_pool("p1", "seed.iso", b"fresh-bytes")

        # Old vol was deleted; a new one created with the fresh data.
        assert old.deleted is True
        assert conn.pool.volumes["seed.iso"] is not old
        assert bytes(conn.streams[0].sent) == b"fresh-bytes"


class TestDownloadFromPool:
    def test_flattens_then_streams_to_dest(self, tmp_path: Path) -> None:
        payload = b"post-install-disk-bytes\x00\xff" * 1024
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.pool.volumes["disk.qcow2"] = _FakeStorageVol("disk.qcow2", contents=payload)
        d._conn = conn  # type: ignore[assignment]

        dest = tmp_path / "out.qcow2"
        out = d.download_from_pool("p1", "disk.qcow2", dest)

        assert out == dest
        assert dest.read_bytes() == payload
        # Flat clone was created in-pool with no backingStore and then deleted.
        clone_name = "disk.qcow2.flat.tmp"
        assert clone_name in conn.pool.volumes
        clone = conn.pool.volumes[clone_name]
        assert clone.deleted is True
        # The XML used for the clone is qcow2 with no <backingStore>.
        clone_xmls = [x for x in conn.pool.create_xmls if clone_name in x]
        assert clone_xmls, "expected createXMLFrom call for flat clone"
        assert "<backingStore>" not in clone_xmls[0]
        assert "<format type='qcow2'/>" in clone_xmls[0]
        # Download was driven against the clone, length=0 → stream-until-EOF.
        assert clone.download_called_with is not None
        _, offset, length, _ = clone.download_called_with
        assert offset == 0
        assert length == 0
        assert conn.streams[0].finished is True

    def test_failure_unlinks_partial_dest_and_deletes_clone(self, tmp_path: Path) -> None:
        d = LibvirtDriver(uri="qemu:///system", pool_root=tmp_path / "pools")
        conn = _FakeConn()
        conn.pool.volumes["disk.qcow2"] = _FakeStorageVol("disk.qcow2", contents=b"x" * 100)
        d._conn = conn  # type: ignore[assignment]

        original_recv = _FakeStream.recvAll

        def patched(_self: _FakeStream, _handler: Any, _opaque: Any) -> None:
            raise RuntimeError("simulated read failure")

        _FakeStream.recvAll = patched  # type: ignore[method-assign]
        dest = tmp_path / "out.qcow2"
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                d.download_from_pool("p1", "disk.qcow2", dest)
        finally:
            _FakeStream.recvAll = original_recv  # type: ignore[method-assign]

        assert not dest.exists()
        assert conn.streams[0].aborted is True
        # Flat clone is cleaned up even on failure.
        assert conn.pool.volumes["disk.qcow2.flat.tmp"].deleted is True
