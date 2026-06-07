"""VM lifecycle + snapshots for the ESXi backend (ESXI-4 / ESXI-6).

``create_vm`` assembles a ``vim.vm.ConfigSpec`` from the orchestrator's staged
disks and ``CreateVM_Task``\\ s it. The composed ``backend_name`` is stamped into
``config.name`` so the name->MoRef recovery (ADR-0008 §6, :meth:`EsxiClient.find_vm`)
finds the VM later for every vmid-keyed op.

Disk model (see ``_storage``): disks live at their pool-folder ref paths and are
attached **in place** (existing-file backing, ``fileOperation`` unset) — the VM
folder holds only the .vmx/nvram/serial log. So a stable ref always denotes the
same file across upload -> create_vm -> download -> delete; no re-resolution.

Firmware (BUILD-1b): ``spec.firmware`` -> ``ConfigSpec.firmware`` (``bios``/``efi``).
The run-phase create MUST reproduce the build firmware or a UEFI-installed disk
won't boot under BIOS.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers.esxi import _naming
from testrange.drivers.esxi.devices import DEFAULT_NIC_MODEL
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.drivers.base import VolumeRef
    from testrange.drivers.esxi._client import EsxiClient
    from testrange.networks.base import BuildNic
    from testrange.vms.spec import VMSpec

_log = get_logger(__name__)

# Guest OS hint — a generic 64-bit Linux. Affects only device defaults/Tools
# heuristics, not correctness; the actual guest is whatever the disk installs.
_GUEST_ID = "otherLinux64Guest"

_SCSI_KEY = 1000
_IDE0_KEY = 200  # ESXi auto-creates IDE controllers 200/201 on CreateVM
_SCSI_RESERVED_UNIT = 7  # the controller's own SCSI id; disks skip it
_SHUTDOWN_POLL_S = 2.0


def _scsi_unit(index: int) -> int:
    """SCSI unit number for the ``index``-th disk (0=OS), skipping the reserved 7."""
    return index if index < _SCSI_RESERVED_UNIT else index + 1


def _disk_device(vim: Any, key: int, ref: str, unit: int) -> Any:
    """A VirtualDisk attaching an existing pool-folder vmdk in place."""
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
        fileName=ref, diskMode="persistent", thinProvisioned=True
    )
    return vim.vm.device.VirtualDisk(
        key=key, controllerKey=_SCSI_KEY, unitNumber=unit, backing=backing
    )


def _add(vim: Any, device: Any) -> Any:
    spec = vim.vm.device.VirtualDeviceSpec()
    spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    spec.device = device
    return spec


def _nic_device(vim: Any, key: int, portgroup: str, mac: str) -> Any:
    backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=portgroup)
    connect = vim.vm.device.VirtualDevice.ConnectInfo(
        startConnected=True, connected=True, allowGuestControl=True
    )
    cls = {
        "vmxnet3": vim.vm.device.VirtualVmxnet3,
        "e1000": vim.vm.device.VirtualE1000,
        "e1000e": vim.vm.device.VirtualE1000e,
    }[DEFAULT_NIC_MODEL]
    return cls(key=key, backing=backing, addressType="manual", macAddress=mac, connectable=connect)


def _cdrom_device(vim: Any, key: int, unit: int, iso_ref: str) -> Any:
    backing = vim.vm.device.VirtualCdrom.IsoBackingInfo(fileName=iso_ref)
    connect = vim.vm.device.VirtualDevice.ConnectInfo(
        startConnected=True, connected=True, allowGuestControl=True
    )
    return vim.vm.device.VirtualCdrom(
        key=key, controllerKey=_IDE0_KEY, unitNumber=unit, backing=backing, connectable=connect
    )


def _serial_device(vim: Any, key: int, file_ref: str) -> Any:
    """A datastore-file-backed serial port — the build-result sink (ESXI-8)."""
    backing = vim.vm.device.VirtualSerialPort.FileBackingInfo(fileName=file_ref)
    return vim.vm.device.VirtualSerialPort(key=key, yieldOnPoll=True, backing=backing)


def create_vm(
    client: EsxiClient,
    backend_name: str,
    spec: VMSpec,
    plan_name: str,
    *,
    os_disk_ref: VolumeRef,
    seed_iso_ref: VolumeRef | None,
    network_refs: dict[str, str],
    data_disk_refs: Sequence[VolumeRef] = (),
    build_nic: BuildNic | None = None,
    boot_media_ref: VolumeRef | None = None,
) -> str:
    """Define a VM on ESXi from the orchestrator's staged disks.

    Build-vs-run follows the orchestrator's *intent* (``build_nic`` set), not a
    backend probe: at build exactly one NIC (the build NIC) is attached and the
    declared ``spec.nics`` stay inert; at run each declared NIC is wired to its
    portgroup with its stable MAC.
    """
    vim = client.vim
    ds = client.datastore_name
    scsi = vim.vm.device.VirtualLsiLogicController(
        key=_SCSI_KEY, busNumber=0, sharedBus=vim.vm.device.VirtualSCSIController.Sharing.noSharing
    )
    devices: list[Any] = [_add(vim, scsi)]

    os_key = -101
    devices.append(_add(vim, _disk_device(vim, os_key, str(os_disk_ref), unit=0)))
    for i, ref in enumerate(data_disk_refs):
        devices.append(_add(vim, _disk_device(vim, -(110 + i), str(ref), unit=_scsi_unit(i + 1))))

    # CDROMs on IDE0. ESXi requires the IDE master (unit 0) be filled before the
    # slave (unit 1) — a slave with no master fails power-on. So pack from unit 0:
    # a bootable installer ISO takes the master; the data seed ISO takes the
    # master when there's no installer, else the slave. (Image-origin run/build
    # carries only the seed → it lands on the master.)
    if boot_media_ref is not None:
        devices.append(_add(vim, _cdrom_device(vim, -301, 0, str(boot_media_ref))))
    if seed_iso_ref is not None:
        seed_unit = 1 if boot_media_ref is not None else 0
        devices.append(_add(vim, _cdrom_device(vim, -302, seed_unit, str(seed_iso_ref))))

    # NICs: one build NIC at build (ADR-0017), else the declared NICs.
    if build_nic is not None:
        pg = network_refs[build_nic.network]
        devices.append(_add(vim, _nic_device(vim, -200, pg, build_nic.mac)))
    else:
        for idx, nic in enumerate(spec.nics):
            mac = _naming.compose_mac(plan_name, spec.name, idx)
            devices.append(
                _add(vim, _nic_device(vim, -(200 + idx), network_refs[nic.network], mac))
            )

    # Datastore-file serial port — the build-result sink reads this file.
    serial_ref = f"[{ds}] {backend_name}/serial0.log"
    devices.append(_add(vim, _serial_device(vim, -400, serial_ref)))

    config = vim.vm.ConfigSpec(
        name=backend_name,
        memoryMB=spec.memory.size_mb,
        numCPUs=spec.cpu.count,
        guestId=_GUEST_ID,
        firmware="efi" if spec.firmware == "uefi" else "bios",
        files=vim.vm.FileInfo(vmPathName=f"[{ds}] {backend_name}"),
        deviceChange=devices,
    )
    # Boot order: OS disk first, then the installer CDROM. An empty installer-
    # origin OS disk has no bootloader, so BIOS/OVMF falls through to the CD and
    # runs the installer; post-install the disk wins and the CD never loops
    # (mirrors the Proxmox order=scsi0 semantics). Device keys are the in-spec
    # temp keys, which the host resolves within the CreateVM call.
    boot_disk = vim.vm.BootOptions.BootableDiskDevice(deviceKey=os_key)
    order: list[Any] = [boot_disk]
    if boot_media_ref is not None:
        order.append(vim.vm.BootOptions.BootableCdromDevice())
    config.bootOptions = vim.vm.BootOptions(bootOrder=order)

    task = client.datacenter.vmFolder.CreateVM_Task(
        config=config, pool=client.resource_pool, host=client.host
    )
    client.wait_for_task(task)
    _log.info("created ESXi vm %s (firmware %s)", backend_name, spec.firmware)
    return f"vm:{backend_name}"


def start_vm(client: EsxiClient, backend_name: str) -> None:
    vm = client.require_vm(backend_name)
    if vm.runtime.powerState == client.vim.VirtualMachine.PowerState.poweredOn:
        return
    client.wait_for_task(vm.PowerOnVM_Task())


def shutdown_vm(client: EsxiClient, backend_name: str, *, timeout: float = 120.0) -> None:
    """Graceful guest shutdown (VMware Tools), hard PowerOff after ``timeout``."""
    vim = client.vim
    vm = client.require_vm(backend_name)
    if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOff:
        return
    try:
        vm.ShutdownGuest()  # fire-and-forget; needs Tools. No task is returned.
    except vim.fault.ToolsUnavailable:
        client.wait_for_task(vm.PowerOffVM_Task())
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOff:
            return
        time.sleep(_SHUTDOWN_POLL_S)
    # Timed out waiting for the guest — hard power off.
    if vm.runtime.powerState != vim.VirtualMachine.PowerState.poweredOff:
        client.wait_for_task(vm.PowerOffVM_Task())


def destroy_vm(client: EsxiClient, backend_name: str) -> None:
    """Power off (if needed) then destroy the VM. Tolerant of absence.

    ``Destroy_Task`` removes the VM's home folder (.vmx/nvram/serial log) and its
    attached disks. By teardown the orchestrator has already captured what it
    needs (``download_from_pool`` runs before destroy), and ``delete_volume``
    tolerates an already-gone disk, so this stays idempotent like the rest of the
    teardown surface. Seed/boot ISOs are CDROM media, not owned disks, so they
    survive for their own ``delete_volume``.
    """
    vim = client.vim
    vm = client.find_vm(backend_name)
    if vm is None:
        _log.debug("destroy_vm(%s): no such VM (already gone)", backend_name)
        return
    if vm.runtime.powerState != vim.VirtualMachine.PowerState.poweredOff:
        try:
            client.wait_for_task(vm.PowerOffVM_Task())
        except Exception as e:  # pragma: no cover - already-off race
            _log.debug("destroy_vm: power off %s failed (likely already off): %s", backend_name, e)
    client.wait_for_task(vm.Destroy_Task())
    _log.info("destroyed ESxi vm %s", backend_name)


def get_vm_power_state(client: EsxiClient, backend_name: str) -> str:
    """The VM's power state in the orchestrator's vocabulary.

    ESXi reports ``poweredOn``/``poweredOff``/``suspended``; the orchestrator
    compares against ``running``/``shutoff``, so map the first two.
    """
    vim = client.vim
    state = client.require_vm(backend_name).runtime.powerState
    if state == vim.VirtualMachine.PowerState.poweredOff:
        return "shutoff"
    if state == vim.VirtualMachine.PowerState.poweredOn:
        return "running"
    return str(state)


# -- snapshots (ESXI-6) ---------------------------------------------------


def _walk_snapshots(tree: Any) -> list[Any]:
    """Flatten a snapshot tree, oldest-first (pre-order by creation)."""
    out: list[Any] = []
    for node in tree:
        out.append(node)
        out.extend(_walk_snapshots(node.childSnapshotList))
    return out


def _snapshot_nodes(client: EsxiClient, vm: Any) -> list[Any]:
    info = vm.snapshot
    if info is None:
        return []
    nodes = _walk_snapshots(info.rootSnapshotList)
    nodes.sort(key=lambda n: n.createTime)
    return nodes


def _find_snapshot(client: EsxiClient, vm: Any, name: str) -> Any | None:
    return next((n.snapshot for n in _snapshot_nodes(client, vm) if n.name == name), None)


def create_snapshot(
    client: EsxiClient,
    vm_backend_name: str,
    name: str,
    description: str = "",
    *,
    mem: bool = False,
) -> None:
    """Snapshot the VM. ``mem=True`` captures running RAM state.

    Raises :class:`DriverError` if ``name`` already exists, per the ABC. Disk-only
    snapshots quiesce nothing (the VM may be off); a memory snapshot requires the
    VM running (ESXi enforces this).
    """
    vm = client.require_vm(vm_backend_name)
    if _find_snapshot(client, vm, name) is not None:
        raise DriverError(f"snapshot {name!r} already exists on vm {vm_backend_name!r}")
    task = vm.CreateSnapshot_Task(name=name, description=description, memory=mem, quiesce=False)
    client.wait_for_task(task)
    _log.info("created snapshot %s on vm %s (mem=%s)", name, vm_backend_name, mem)


def list_snapshots(client: EsxiClient, vm_backend_name: str) -> list[str]:
    vm = client.require_vm(vm_backend_name)
    return [n.name for n in _snapshot_nodes(client, vm)]


def delete_snapshot(client: EsxiClient, vm_backend_name: str, name: str) -> None:
    """Delete a snapshot. No-op if ``name`` doesn't exist (per the ABC)."""
    vm = client.require_vm(vm_backend_name)
    snap = _find_snapshot(client, vm, name)
    if snap is None:
        return
    client.wait_for_task(snap.RemoveSnapshot_Task(removeChildren=False))
    _log.info("deleted snapshot %s on vm %s", name, vm_backend_name)


def restore_snapshot(client: EsxiClient, vm_backend_name: str, name: str) -> None:
    """Revert the VM to ``name``. Raises :class:`DriverError` if it's absent.

    A disk-only snapshot leaves the VM off after revert; a memory snapshot
    restores the running state (ESXi resumes it as part of the revert).
    """
    vm = client.require_vm(vm_backend_name)
    snap = _find_snapshot(client, vm, name)
    if snap is None:
        raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}")
    client.wait_for_task(snap.RevertToSnapshot_Task())
    _log.info("reverted vm %s to snapshot %s", vm_backend_name, name)
