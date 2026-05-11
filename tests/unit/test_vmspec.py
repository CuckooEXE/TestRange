"""Tests for VMSpec singleton-device constraints."""

from __future__ import annotations

import pytest

from testrange.devices import CPU, HardDrive, LibvirtNetworkIface, Memory, OSDrive
from testrange.vms import VMSpec


def _basic_devices() -> list:
    return [CPU(2), Memory(1024), OSDrive("p1", 8), LibvirtNetworkIface("netA")]


class TestVMSpec:
    def test_valid(self) -> None:
        s = VMSpec(name="web", devices=_basic_devices())
        assert s.name == "web"
        assert s.cpu.count == 2
        assert s.memory.size_mb == 1024
        assert s.os_drive.pool == "p1"
        assert len(s.nics) == 1
        assert s.data_drives == ()

    def test_multiple_cpus(self) -> None:
        with pytest.raises(ValueError, match="exactly one CPU"):
            VMSpec(name="x", devices=[CPU(1), CPU(2), Memory(512), OSDrive("p1", 4)])

    def test_no_cpu(self) -> None:
        with pytest.raises(ValueError, match="exactly one CPU"):
            VMSpec(name="x", devices=[Memory(512), OSDrive("p1", 4)])

    def test_multiple_memory(self) -> None:
        with pytest.raises(ValueError, match="exactly one Memory"):
            VMSpec(name="x", devices=[CPU(1), Memory(512), Memory(1024), OSDrive("p1", 4)])

    def test_multiple_osdrive(self) -> None:
        with pytest.raises(ValueError, match="exactly one OSDrive"):
            VMSpec(
                name="x",
                devices=[CPU(1), Memory(512), OSDrive("p1", 4), OSDrive("p2", 4)],
            )

    def test_multiple_data_drives_ok(self) -> None:
        s = VMSpec(
            name="x",
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("p1", 4),
                HardDrive("p2", 100),
                HardDrive("p2", 200),
            ],
        )
        assert len(s.data_drives) == 2

    def test_no_nic_ok(self) -> None:
        s = VMSpec(name="x", devices=[CPU(1), Memory(512), OSDrive("p1", 4)])
        assert s.nics == ()
