"""Tests for Proxmox-specific devices — ProxmoxHardDrive controller-bus selection."""

from __future__ import annotations

import pytest

from testrange.devices import CPU, HardDrive, Memory, OSDrive
from testrange.drivers.proxmox import ProxmoxHardDrive
from testrange.vms import VMSpec


class TestProxmoxHardDrive:
    def test_default_bus_is_scsi(self) -> None:
        assert ProxmoxHardDrive("pool1", 8).bus == "scsi"

    def test_every_supported_bus_is_accepted(self) -> None:
        for bus in ("scsi", "virtio", "sata", "ide"):
            assert ProxmoxHardDrive("pool1", 8, bus=bus).bus == bus

    def test_unknown_bus_rejected(self) -> None:
        with pytest.raises(ValueError, match="bus must be one of"):
            ProxmoxHardDrive("pool1", 8, bus="nvme")

    def test_inherits_base_disk_validation(self) -> None:
        with pytest.raises(ValueError, match="size_gb"):
            ProxmoxHardDrive("pool1", 0, bus="virtio")

    def test_is_a_harddrive_so_it_flows_into_vmspec(self) -> None:
        # VMSpec.data_drives collects by isinstance(HardDrive); a subclass counts.
        spec = VMSpec(
            name="v",
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                HardDrive("pool1", 1),
                ProxmoxHardDrive("pool1", 1, bus="virtio"),
            ],
        )
        assert len(spec.data_drives) == 2
        assert isinstance(spec.data_drives[1], ProxmoxHardDrive)
        assert spec.data_drives[1].bus == "virtio"
