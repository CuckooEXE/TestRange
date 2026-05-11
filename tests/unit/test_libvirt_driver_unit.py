"""Unit tests for LibvirtDriver — naming/MAC/preflight/XML rendering.

Connection + live libvirt calls are exercised in tests/integration/.
"""

from __future__ import annotations

import re
from pathlib import Path

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
