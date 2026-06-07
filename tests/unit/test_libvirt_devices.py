"""Tests for libvirt-specific devices — disk ``bus`` + NIC ``model`` selection.

The concretes live in ``testrange.devices.disk.libvirt`` /
``testrange.devices.network.libvirt`` and subclass the portable types so they
still flow into ``VMSpec`` via the isinstance accessors.
"""

from __future__ import annotations

import pytest

from testrange.devices import CPU, HardDrive, Memory, NetworkIface, OSDrive
from testrange.devices.disk.base import _Disk
from testrange.devices.disk.libvirt import LibvirtDataDrive, LibvirtOSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.vms import VMSpec


class TestLibvirtDisks:
    def test_default_bus_is_virtio(self) -> None:
        assert LibvirtOSDrive("pool1", 8).bus == "virtio"
        assert LibvirtDataDrive("pool1", 8).bus == "virtio"

    def test_every_supported_bus_accepted(self) -> None:
        for bus in ("virtio", "sata", "ide", "scsi"):
            assert LibvirtOSDrive("pool1", 8, bus=bus).bus == bus

    def test_unknown_bus_rejected(self) -> None:
        with pytest.raises(ValueError, match="bus must be one of"):
            LibvirtOSDrive("pool1", 8, bus="nvme")

    def test_inherits_base_disk_validation(self) -> None:
        # super().__post_init__() runs first, so the _Disk invariants still bite.
        with pytest.raises(ValueError, match="size_gb"):
            LibvirtOSDrive("pool1", 0, bus="sata")
        with pytest.raises(ValueError, match="pool"):
            LibvirtDataDrive("", 8)

    def test_subclass_identity_flows_into_vmspec(self) -> None:
        spec = VMSpec(
            name="esxi",
            devices=[
                CPU(2),
                Memory(4096),
                LibvirtOSDrive("pool1", 33, bus="sata"),
                HardDrive("pool1", 4),
                LibvirtDataDrive("pool1", 4, bus="ide"),
            ],
        )
        # LibvirtOSDrive IS an OSDrive / _Disk; LibvirtDataDrive IS a HardDrive.
        assert isinstance(spec.os_drive, LibvirtOSDrive) and spec.os_drive.bus == "sata"
        assert isinstance(spec.os_drive, _Disk)
        assert len(spec.data_drives) == 2
        assert spec.data_drives[1].bus == "ide"  # type: ignore[attr-defined]


class TestLibvirtNIC:
    def test_default_model_is_virtio(self) -> None:
        assert LibvirtNetworkIface("lab").model == "virtio"

    def test_every_supported_model_accepted(self) -> None:
        for model in ("virtio", "e1000", "e1000e", "rtl8139"):
            assert LibvirtNetworkIface("lab", model=model).model == model

    def test_unknown_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="model must be one of"):
            LibvirtNetworkIface("lab", model="vmxnet3")

    def test_inherits_base_nic_validation(self) -> None:
        with pytest.raises(ValueError, match="network must be a non-empty"):
            LibvirtNetworkIface("", model="e1000e")

    def test_is_a_networkiface_in_vmspec(self) -> None:
        spec = VMSpec(
            name="esxi",
            devices=[
                CPU(2),
                Memory(4096),
                OSDrive("pool1", 8),
                NetworkIface("a"),
                LibvirtNetworkIface("b", model="e1000e"),
            ],
        )
        assert len(spec.nics) == 2
        assert isinstance(spec.nics[1], LibvirtNetworkIface)
        assert spec.nics[1].model == "e1000e"
