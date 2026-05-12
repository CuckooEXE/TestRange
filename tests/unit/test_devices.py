"""Tests for device dataclasses (CPU/Memory/disks/NIC/Pool)."""

from __future__ import annotations

import pytest

from testrange.devices import (
    CPU,
    HardDrive,
    Memory,
    NetworkIface,
    OSDrive,
    StoragePool,
)
from testrange.devices.network.libvirt import LibvirtNetworkIface


class TestCPU:
    def test_valid(self) -> None:
        c = CPU(4)
        assert c.count == 4

    @pytest.mark.parametrize("bad", [0, -1, "two", None, 1.5])
    def test_invalid_count(self, bad: object) -> None:
        with pytest.raises(ValueError):
            CPU(bad)  # type: ignore[arg-type]


class TestMemory:
    def test_valid(self) -> None:
        m = Memory(2048)
        assert m.size_mb == 2048

    @pytest.mark.parametrize("bad", [0, -1, None])
    def test_invalid(self, bad: object) -> None:
        with pytest.raises(ValueError):
            Memory(bad)  # type: ignore[arg-type]


class TestDisks:
    def test_os_drive(self) -> None:
        d = OSDrive("pool1", 16)
        assert d.pool == "pool1"
        assert d.size_gb == 16

    def test_hard_drive(self) -> None:
        d = HardDrive("pool2", 100)
        assert d.size_gb == 100

    def test_invalid_pool(self) -> None:
        with pytest.raises(ValueError):
            OSDrive("", 8)

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError):
            OSDrive("pool1", 0)


class TestNICs:
    def test_libvirt_iface(self) -> None:
        n = LibvirtNetworkIface("netA")
        assert n.network == "netA"
        assert n.driver == "virtio"
        assert isinstance(n, NetworkIface)

    def test_libvirt_iface_with_driver(self) -> None:
        n = LibvirtNetworkIface("netA", driver="e1000")
        assert n.driver == "e1000"

    def test_invalid_network(self) -> None:
        with pytest.raises(ValueError):
            LibvirtNetworkIface("")


class TestPool:
    def test_valid(self) -> None:
        p = StoragePool("p1", 32)
        assert p.size_gb == 32

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError):
            StoragePool("p1", 0)
