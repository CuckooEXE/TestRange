"""ESXI-4/6/8: VM lifecycle, ConfigSpec assembly, snapshots, serial sink (fakes)."""

from __future__ import annotations

from typing import Any

import pytest

from testrange.devices import CPU, HardDrive, Memory, OSDrive
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.drivers.base import VolumeRef
from testrange.drivers.esxi._client import EsxiConn
from testrange.drivers.esxi.driver import ESXiDriver
from testrange.exceptions import DriverError
from testrange.networks import Network, Switch
from testrange.networks.base import BuildNic, NetworkAddressing
from testrange.vms import VMSpec
from tests.esxi_fakes import FakeEsxiClient


def _driver(client: FakeEsxiClient) -> ESXiDriver:
    return ESXiDriver(EsxiConn(host="h", datastore="datastore1"), client=client)  # type: ignore[arg-type]


def _spec(name: str, *extra: Any) -> VMSpec:
    return VMSpec(name=name, devices=[CPU(2), Memory(1024), OSDrive("pool1", 8), *extra])


def _devtypes(config: object) -> list[str]:
    return [d.device._vimtype for d in config.deviceChange]  # type: ignore[attr-defined]


def test_run_vm_assembles_controller_disk_nics_serial() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    spec = _spec("web", NetworkIface("netA", addr=StaticAddr("10.0.0.5/24")))
    d.create_vm(
        "tr-vm-web",
        spec,
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/web.vmdk"),
        seed_iso_ref=VolumeRef("[datastore1] pool1/web-seed.iso"),
        network_refs={"netA": "trp-abc"},
    )
    vm = client.find_vm("tr-vm-web")
    assert vm is not None
    types = _devtypes(vm.config_spec)
    assert "vm.device.VirtualLsiLogicController" in types
    assert "vm.device.VirtualDisk" in types
    assert "vm.device.VirtualVmxnet3" in types
    assert "vm.device.VirtualSerialPort" in types
    assert "vm.device.VirtualCdrom" in types  # seed ISO
    assert vm.config_spec.firmware == "bios"


def test_build_vm_attaches_only_build_nic() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    spec = _spec(
        "web",
        NetworkIface("netA", addr=StaticAddr("10.0.0.5/24")),
        NetworkIface("netB"),
    )
    addr = StaticAddr("10.97.99.3/24")
    switch = Switch("build", Network("build-net"), cidr="10.97.99.0/24")
    bnic = BuildNic(
        mac="00:50:56:01:02:03",
        network="build-net",
        addr=addr,
        addressing=NetworkAddressing.from_switch(switch),
    )
    d.create_vm(
        "tr-build-web",
        spec,
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/web.vmdk"),
        seed_iso_ref=None,
        network_refs={"build-net": "trp-build"},
        build_nic=bnic,
    )
    vm = client.find_vm("tr-build-web")
    nics = [d for d in vm.config_spec.deviceChange if "Vmxnet3" in d.device._vimtype]
    assert len(nics) == 1, "build VM must attach exactly the build NIC"
    assert nics[0].device.macAddress == "00:50:56:01:02:03"


def test_data_disks_in_spec_order() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    spec = _spec("fs", HardDrive("pool1", 2), HardDrive("pool1", 2))
    d.create_vm(
        "tr-vm-fs",
        spec,
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/fs.vmdk"),
        seed_iso_ref=None,
        network_refs={},
        data_disk_refs=[
            VolumeRef("[datastore1] pool1/fs-data0.vmdk"),
            VolumeRef("[datastore1] pool1/fs-data1.vmdk"),
        ],
    )
    vm = client.find_vm("tr-vm-fs")
    disks = [d for d in vm.config_spec.deviceChange if d.device._vimtype == "vm.device.VirtualDisk"]
    assert len(disks) == 3  # OS + 2 data
    units = sorted(d.device.unitNumber for d in disks)
    assert units == [0, 1, 2]


def _disks_by_controller(vm: Any) -> dict[int, list[int]]:
    """Map controllerKey -> sorted unit numbers of the VirtualDisks attached to it."""
    out: dict[int, list[int]] = {}
    for ch in vm.config_spec.deviceChange:
        if ch.device._vimtype == "vm.device.VirtualDisk":
            out.setdefault(ch.device.controllerKey, []).append(ch.device.unitNumber)
    return {k: sorted(v) for k, v in out.items()}


def test_data_disk_bus_selects_per_controller() -> None:
    # ESXI-20: ESXiHardDrive.bus picks the controller. OS (scsi) + a scsi/sata/nvme
    # data disk -> the OS+scsi share the LsiLogic controller (key 1000), sata gets
    # an AHCI controller, nvme gets an NVMe controller. Mirrors tests/plans/esxi.
    from testrange.drivers.esxi import _vm
    from testrange.drivers.esxi.devices import ESXiHardDrive

    client = FakeEsxiClient()
    d = _driver(client)
    spec = _spec(
        "buses",
        ESXiHardDrive("pool1", 1, bus="scsi"),
        ESXiHardDrive("pool1", 1, bus="sata"),
        ESXiHardDrive("pool1", 1, bus="nvme"),
    )
    d.create_vm(
        "tr-vm-buses",
        spec,
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/buses.vmdk"),
        seed_iso_ref=None,
        network_refs={},
        data_disk_refs=[
            VolumeRef("[datastore1] pool1/buses-data0.vmdk"),
            VolumeRef("[datastore1] pool1/buses-data1.vmdk"),
            VolumeRef("[datastore1] pool1/buses-data2.vmdk"),
        ],
    )
    vm = client.find_vm("tr-vm-buses")
    types = _devtypes(vm.config_spec)
    # exactly one of each add-on controller, materialized on first use.
    assert types.count("vm.device.VirtualAHCIController") == 1
    assert types.count("vm.device.VirtualNVMEController") == 1
    by_ctrl = _disks_by_controller(vm)
    assert by_ctrl[_vm._SCSI_KEY] == [0, 1]  # OS at unit 0 + the scsi data disk at 1
    assert by_ctrl[_vm._SATA_KEY] == [0]  # the sata data disk
    assert by_ctrl[_vm._NVME_KEY] == [0]  # the nvme data disk


def test_scsi_data_disks_skip_reserved_unit_7() -> None:
    # Seven scsi data disks land on the LsiLogic controller after the OS disk at 0,
    # so the eighth would be unit 7 (the controller's own id) and must skip to 8.
    from testrange.drivers.esxi import _vm
    from testrange.drivers.esxi.devices import ESXiHardDrive

    client = FakeEsxiClient()
    d = _driver(client)
    data = [ESXiHardDrive("pool1", 1, bus="scsi") for _ in range(7)]
    refs = [VolumeRef(f"[datastore1] pool1/d{i}.vmdk") for i in range(7)]
    d.create_vm(
        "tr-vm-many",
        _spec("many", *data),
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/many.vmdk"),
        seed_iso_ref=None,
        network_refs={},
        data_disk_refs=refs,
    )
    vm = client.find_vm("tr-vm-many")
    units = _disks_by_controller(vm)[_vm._SCSI_KEY]
    assert units == [0, 1, 2, 3, 4, 5, 6, 8], f"unit 7 must be skipped, got {units}"


def test_plain_hard_drive_defaults_to_scsi() -> None:
    # A portable HardDrive (no bus) keeps the historical behavior: all on SCSI.
    from testrange.drivers.esxi import _vm

    client = FakeEsxiClient()
    d = _driver(client)
    d.create_vm(
        "tr-vm-plain",
        _spec("plain", HardDrive("pool1", 2), HardDrive("pool1", 2)),
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/plain.vmdk"),
        seed_iso_ref=None,
        network_refs={},
        data_disk_refs=[
            VolumeRef("[datastore1] pool1/plain-d0.vmdk"),
            VolumeRef("[datastore1] pool1/plain-d1.vmdk"),
        ],
    )
    vm = client.find_vm("tr-vm-plain")
    assert _devtypes(vm.config_spec).count("vm.device.VirtualAHCIController") == 0
    assert _disks_by_controller(vm) == {_vm._SCSI_KEY: [0, 1, 2]}


def test_installer_origin_boot_order_falls_through_to_cdrom() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    spec = _spec("inst")
    spec = VMSpec(name="inst", devices=[CPU(1), Memory(512), OSDrive("pool1", 8)], firmware="uefi")
    d.create_vm(
        "tr-build-inst",
        spec,
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/inst.vmdk"),
        seed_iso_ref=None,
        network_refs={},
        boot_media_ref=VolumeRef("[datastore1] pool1/installer.iso"),
    )
    vm = client.find_vm("tr-build-inst")
    assert vm.config_spec.firmware == "efi"
    order = vm.config_spec.bootOptions.bootOrder
    assert len(order) == 2  # disk then cdrom


def test_power_lifecycle() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    d.create_vm(
        "tr-vm-p",
        _spec("p"),
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/p.vmdk"),
        seed_iso_ref=None,
        network_refs={},
    )
    assert d.get_vm_power_state("tr-vm-p") == "shutoff"
    d.start_vm("tr-vm-p")
    assert d.get_vm_power_state("tr-vm-p") == "running"
    d.shutdown_vm("tr-vm-p", timeout=1.0)
    assert d.get_vm_power_state("tr-vm-p") == "shutoff"


def test_destroy_vm_tolerant_of_absence() -> None:
    client = FakeEsxiClient()
    _driver(client).destroy_vm("nope")  # no such VM -> no-op


def test_serial_sink_yields_then_ends_on_poweroff() -> None:
    client = FakeEsxiClient()
    d = _driver(client)
    d.create_vm(
        "tr-build-s",
        _spec("s"),
        "plan",
        os_disk_ref=VolumeRef("[datastore1] pool1/s.vmdk"),
        seed_iso_ref=None,
        network_refs={},
    )
    vm = client.find_vm("tr-build-s")
    vm._power = client.vim.VirtualMachine.PowerState.poweredOn
    client.files["tr-build-s/serial0.log"] = b"TESTRANGE-RESULT: ok\n"
    gen = d.read_build_result_sink("tr-build-s")
    first = next(gen)
    assert first == b"TESTRANGE-RESULT: ok\n"
    # power off -> the generator drains and ends
    vm._power = client.vim.VirtualMachine.PowerState.poweredOff
    rest = list(gen)
    assert rest == [] or all(isinstance(c, bytes) for c in rest)


class TestSnapshots:
    def _vm_with_snaps(self, client: FakeEsxiClient) -> None:
        from types import SimpleNamespace

        d = _driver(client)
        d.create_vm(
            "tr-vm-snap",
            _spec("snap"),
            "plan",
            os_disk_ref=VolumeRef("[datastore1] pool1/snap.vmdk"),
            seed_iso_ref=None,
            network_refs={},
        )
        vm = client.find_vm("tr-vm-snap")

        # Model CreateSnapshot_Task/list/remove/revert on the fake VM.
        tree: list[Any] = []

        def create(name: str, description: str, memory: bool, quiesce: bool) -> object:
            from tests.esxi_fakes import _FakeTask

            node = SimpleNamespace(
                name=name,
                createTime=len(tree),
                childSnapshotList=[],
                snapshot=SimpleNamespace(),
            )
            node.snapshot.RemoveSnapshot_Task = lambda removeChildren: _remove(node)
            node.snapshot.RevertToSnapshot_Task = lambda: _FakeTask()
            tree.append(node)
            vm.snapshot = SimpleNamespace(rootSnapshotList=list(tree))
            return _FakeTask()

        def _remove(node: object) -> object:
            from tests.esxi_fakes import _FakeTask

            tree.remove(node)
            vm.snapshot = SimpleNamespace(rootSnapshotList=list(tree)) if tree else None
            return _FakeTask()

        vm.CreateSnapshot_Task = create
        vm.PowerOnVM_Task()  # mem=True snapshots require a running VM (ABC contract)

    def test_create_list_delete_restore(self) -> None:
        client = FakeEsxiClient()
        self._vm_with_snaps(client)
        d = _driver(client)
        d.create_snapshot("tr-vm-snap", "s1", "first")
        d.create_snapshot("tr-vm-snap", "s2", mem=True)
        assert d.list_snapshots("tr-vm-snap") == ["s1", "s2"]
        d.delete_snapshot("tr-vm-snap", "s1")
        assert d.list_snapshots("tr-vm-snap") == ["s2"]
        d.restore_snapshot("tr-vm-snap", "s2")  # no raise
        d.delete_snapshot("tr-vm-snap", "absent")  # no-op

    def test_memory_snapshot_on_powered_off_vm_raises(self) -> None:
        # ABC contract: mem=True requires a running VM (no RAM to capture off).
        client = FakeEsxiClient()
        self._vm_with_snaps(client)
        client.find_vm("tr-vm-snap").PowerOffVM_Task()
        d = _driver(client)
        with pytest.raises(DriverError, match="to be running"):
            d.create_snapshot("tr-vm-snap", "s1", mem=True)

    def test_duplicate_snapshot_raises(self) -> None:
        from testrange.exceptions import DriverError

        client = FakeEsxiClient()
        self._vm_with_snaps(client)
        d = _driver(client)
        d.create_snapshot("tr-vm-snap", "s1")
        with pytest.raises(DriverError, match="already exists"):
            d.create_snapshot("tr-vm-snap", "s1")

    def test_restore_missing_raises(self) -> None:
        from testrange.exceptions import DriverError

        client = FakeEsxiClient()
        self._vm_with_snaps(client)
        d = _driver(client)
        with pytest.raises(DriverError, match="not found"):
            d.restore_snapshot("tr-vm-snap", "ghost")
