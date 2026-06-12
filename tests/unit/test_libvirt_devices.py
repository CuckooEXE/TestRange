"""Tests for libvirt-specific devices — disk ``bus`` + NIC ``model`` selection.

The concretes live in ``testrange.devices.disk.libvirt`` /
``testrange.devices.network.libvirt`` and subclass the portable types so they
still flow into ``VMSpec`` via the isinstance accessors. Pool/network
references are typed handles (ADR-0030), constructed directly here since no
Hypervisor container is in play.
"""

from __future__ import annotations

import pytest

from testrange.devices import CPU, HardDrive, Memory, NetworkIface, OSDrive
from testrange.devices.disk.base import _Disk
from testrange.devices.disk.libvirt import LibvirtDataDrive, LibvirtOSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.handles import NetworkHandle, PoolHandle
from testrange.vms import VMSpec

_POOL = PoolHandle("pool1")
_LAB = NetworkHandle("lab", switch="sw1")


class TestLibvirtDisks:
    def test_default_bus_is_virtio(self) -> None:
        assert LibvirtOSDrive(_POOL, 8).bus == "virtio"
        assert LibvirtDataDrive(_POOL, 8).bus == "virtio"

    def test_every_supported_bus_accepted(self) -> None:
        for bus in ("virtio", "sata", "ide", "scsi"):
            assert LibvirtOSDrive(_POOL, 8, bus=bus).bus == bus

    def test_unknown_bus_rejected(self) -> None:
        with pytest.raises(ValueError, match="bus must be one of"):
            LibvirtOSDrive(_POOL, 8, bus="nvme")

    def test_inherits_base_disk_validation(self) -> None:
        # super().__post_init__() runs first, so the _Disk invariants still bite.
        with pytest.raises(ValueError, match="size_gb"):
            LibvirtOSDrive(_POOL, 0, bus="sata")
        with pytest.raises(TypeError, match="PoolHandle"):
            LibvirtDataDrive("pool1", 8)  # type: ignore[arg-type]  # bare string, not a handle

    def test_subclass_identity_flows_into_vmspec(self) -> None:
        spec = VMSpec(
            name="esxi",
            devices=[
                CPU(2),
                Memory(4096),
                LibvirtOSDrive(_POOL, 33, bus="sata"),
                HardDrive(_POOL, 4),
                LibvirtDataDrive(_POOL, 4, bus="ide"),
            ],
        )
        # LibvirtOSDrive IS an OSDrive / _Disk; LibvirtDataDrive IS a HardDrive.
        assert isinstance(spec.os_drive, LibvirtOSDrive) and spec.os_drive.bus == "sata"
        assert isinstance(spec.os_drive, _Disk)
        assert len(spec.data_drives) == 2
        assert spec.data_drives[1].bus == "ide"  # type: ignore[attr-defined]


class TestLibvirtNIC:
    def test_default_model_is_virtio(self) -> None:
        assert LibvirtNetworkIface(_LAB).model == "virtio"

    def test_every_supported_model_accepted(self) -> None:
        for model in ("virtio", "e1000", "e1000e", "rtl8139"):
            assert LibvirtNetworkIface(_LAB, model=model).model == model

    def test_unknown_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="model must be one of"):
            LibvirtNetworkIface(_LAB, model="vmxnet3")

    def test_inherits_base_nic_validation(self) -> None:
        with pytest.raises(TypeError, match="NetworkHandle"):
            LibvirtNetworkIface("lab", model="e1000e")  # type: ignore[arg-type]  # bare string

    def test_is_a_networkiface_in_vmspec(self) -> None:
        spec = VMSpec(
            name="esxi",
            devices=[
                CPU(2),
                Memory(4096),
                OSDrive(_POOL, 8),
                NetworkIface(NetworkHandle("a", switch="sw1")),
                LibvirtNetworkIface(NetworkHandle("b", switch="sw1"), model="e1000e"),
            ],
        )
        assert len(spec.nics) == 2
        assert isinstance(spec.nics[1], LibvirtNetworkIface)
        assert spec.nics[1].model == "e1000e"
