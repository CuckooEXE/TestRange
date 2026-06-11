"""Datastore pool + volume I/O for the ESXi backend (ESXI-3).

Canonical cache format is **qcow2** cache-wide (decision A); the ESXi driver
converts only at this boundary (CORE-2 / ADR-0024). Two transports cross it:

- **bytes** ride the datastore ``/folder`` HTTPS channel (PUT/GET) — ESXi has no
  SOAP byte-egress for datastore files;
- **disk realization** (blank/copy/extend/delete) is the pyVmomi
  ``VirtualDiskManager``, producing managed VMFS disks that are bootable and
  growable.

Disk model (simpler than Proxmox's Option-2)
--------------------------------------------
A pool is a **datastore folder** (``[ds] <pool>/``). A volume's ``VolumeRef`` is
its real datastore path ``[ds] <pool>/<name>.vmdk`` (or ``.iso``). The disk lives
*at that path* and ``create_vm`` attaches it **in place** (the VM folder holds
only the .vmx/nvram/serial log), so the stable ref the orchestrator threads
through ``upload -> create_vm -> download -> delete`` always denotes the same
file — no vm-scoped re-resolution. Every ABC method does exactly what it says:
``create_blank_volume`` really creates the VMFS disk, ``resize_volume`` really
extends it.

Ingest (``upload_to_pool``, the S2 crux)
----------------------------------------
1. ``qemu-img`` qcow2 -> vmdk ``monolithicSparse`` (single self-contained file)
   on the orchestrator host;
2. ``/folder`` PUT it to a staging path on the datastore;
3. ``VirtualDiskManager.CopyVirtualDisk_Task`` inflates the hosted-sparse staging
   into a managed VMFS **thin** disk at the ref (bootable + growable);
4. delete the staging file.
Idempotent: a disk already at the ref is left as-is.

Egress (``download_from_pool``)
-------------------------------
The disk at the ref *is* the one the VM wrote (attached in place), so after the
build VM powers off we ``/folder`` GET its descriptor + ``-flat`` extent and
``qemu-img`` vmdk -> qcow2. Self-contained (no backing chain — the ABC invariant).
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers import _diskconvert
from testrange.drivers.base import VolumeRef
from testrange.drivers.esxi import _naming
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.devices.pool.base import StoragePool
    from testrange.drivers.esxi._client import EsxiClient

_log = get_logger(__name__)

_KB_PER_GB = 1024 * 1024
# lsiLogic is the broadly-compatible controller every supported guest has a
# driver for; the VM's OS-disk controller (ESXI-4) matches it.
_ADAPTER = "lsiLogic"


def _dc(client: EsxiClient) -> Any:
    return client.datacenter


def _vdm(client: EsxiClient) -> Any:
    return client.content.virtualDiskManager


def _ds_path(client: EsxiClient, ref: str) -> str:
    """The datastore-relative path (``pool/name.vmdk``) for a ref on this store.

    Guards that the ref targets the connected datastore — a cross-datastore ref
    would silently miss the /folder transport.
    """
    ds, path = _naming.parse_volume_ref(ref)
    if ds != client.datastore_name:
        raise DriverError(
            f"volume ref {ref!r} targets datastore {ds!r}, not the connected "
            f"{client.datastore_name!r}"
        )
    return path


def create_pool(client: EsxiClient, pool: StoragePool, backend_name: str) -> str:
    """Create the pool's datastore folder (``[ds] <backend_name>``). Idempotent."""
    del pool
    vim = client.vim
    path = f"[{client.datastore_name}] {backend_name}"
    with contextlib.suppress(vim.fault.FileAlreadyExists):
        client.content.fileManager.MakeDirectory(
            name=path, datacenter=_dc(client), createParentDirectories=True
        )
    _log.info("pool folder %s ready", path)
    return f"pool:{backend_name}"


def destroy_pool(client: EsxiClient, backend_name: str) -> None:
    """Delete the pool's datastore folder and everything under it. Tolerant of absence.

    "Already gone" can surface two ways — a synchronous ``FileNotFound`` fault or
    a *task* that fails with a not-found message (``DeleteDatastoreFile_Task`` runs
    async). Both are success for an idempotent teardown; any other failure
    propagates.
    """
    vim = client.vim
    path = f"[{client.datastore_name}] {backend_name}"
    try:
        task = client.content.fileManager.DeleteDatastoreFile_Task(
            name=path, datacenter=_dc(client)
        )
        client.wait_for_task(task)
    except vim.fault.FileNotFound:
        _log.debug("destroy_pool(%s): folder already gone", backend_name)
    except DriverError as e:
        if "not found" not in str(e).lower() and "was not found" not in str(e).lower():
            raise
        _log.debug("destroy_pool(%s): folder already gone (task)", backend_name)


def write_to_pool(client: EsxiClient, target_ref: VolumeRef, data: bytes) -> VolumeRef:
    """Write raw bytes as the datastore file the ref names (seed/boot ISO).

    Replace-if-exists. Staged to a temp file and /folder PUT into the pool folder
    (the folder is created by ``create_pool`` first).
    """
    ds_path = _ds_path(client, str(target_ref))
    with tempfile.NamedTemporaryFile(prefix="tr_esxi_write_", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        client.folder_put(tmp_path, ds_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return target_ref


def create_blank_volume(client: EsxiClient, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """Provision a blank, sized VMFS thin disk at the ref. Replace-if-exists."""
    ds_path = _ds_path(client, str(target_ref))
    if client.folder_exists(ds_path):
        delete_volume(client, target_ref)
    vim = client.vim
    spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(
        capacityKb=size_gb * _KB_PER_GB, diskType="thin", adapterType=_ADAPTER
    )
    task = _vdm(client).CreateVirtualDisk_Task(
        name=str(target_ref), datacenter=_dc(client), spec=spec
    )
    client.wait_for_task(task)
    _log.info("created blank VMFS disk %s (%d GiB)", target_ref, size_gb)
    return target_ref


def resize_volume(client: EsxiClient, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """Grow the VMFS disk at the ref to ``size_gb`` (ExtendVirtualDisk)."""
    task = _vdm(client).ExtendVirtualDisk_Task(
        name=str(target_ref),
        datacenter=_dc(client),
        newCapacityKb=size_gb * _KB_PER_GB,
        eagerZero=False,
    )
    client.wait_for_task(task)
    _log.info("extended VMFS disk %s to %d GiB", target_ref, size_gb)
    return target_ref


def upload_to_pool(client: EsxiClient, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
    """Ingest a qcow2 cache file as a bootable+growable VMFS disk at the ref.

    The S2 path: qemu-img qcow2 -> monolithicSparse vmdk (host), /folder PUT to a
    staging path, CopyVirtualDisk inflate to a managed VMFS thin disk, drop the
    staging. Idempotent per the ABC contract: a disk already at the ref is
    returned without re-ingesting (a retry/resume must not re-transfer a multi-GB
    image).
    """
    ds_path = _ds_path(client, str(target_ref))
    if client.folder_exists(ds_path):
        _log.info("upload_to_pool: %s already present; skipping re-ingest", target_ref)
        return target_ref

    stage_rel = f"{ds_path.rsplit('.', 1)[0]}-stage.vmdk"
    stage_ref = f"[{client.datastore_name}] {stage_rel}"
    # Stage on the cache filesystem (next to the qcow2 source), not the default
    # tempdir: the monolithicSparse conversion is disk-sized (multi-GiB) and /tmp
    # is a small tmpfs on most hosts — a parallel ingest would overflow it
    # ([Errno 28]). Same-fs staging also keeps the qemu-img convert single-device.
    with tempfile.TemporaryDirectory(prefix="tr_esxi_ingest_", dir=source_path.parent) as tmp:
        local_vmdk = Path(tmp) / "stage.vmdk"
        _diskconvert.qcow2_to_vmdk(source_path, local_vmdk, subformat="monolithicSparse")
        client.folder_put(local_vmdk, stage_rel)
    try:
        vim = client.vim
        spec = vim.VirtualDiskManager.VirtualDiskSpec(diskType="thin", adapterType=_ADAPTER)
        task = _vdm(client).CopyVirtualDisk_Task(
            sourceName=stage_ref,
            sourceDatacenter=_dc(client),
            destName=str(target_ref),
            destDatacenter=_dc(client),
            destSpec=spec,
            force=True,
        )
        client.wait_for_task(task)
    finally:
        client.folder_delete(stage_rel)
        client.folder_delete(f"{stage_rel.rsplit('.', 1)[0]}-flat.vmdk")
    _log.info("ingested %s -> VMFS disk %s", source_path.name, target_ref)
    return target_ref


def download_from_pool(client: EsxiClient, vol_ref: VolumeRef, dest_path: Path) -> Path:
    """Stream the disk at the ref back to the orchestrator host as qcow2.

    The disk was attached in place, so the ref denotes the exact file the VM
    wrote. GET the descriptor + ``-flat`` extent and qemu-img vmdk -> qcow2. The
    VM is powered off by the orchestrator before this, so the read is consistent.
    """
    ds_path = _ds_path(client, str(vol_ref))
    descriptor_rel = ds_path
    flat_rel = f"{ds_path.rsplit('.', 1)[0]}-flat.vmdk"
    # Stage on the cache filesystem (next to the qcow2 destination), not the
    # default tempdir: the exported -flat extent is disk-sized (multi-GiB) and a
    # parallel export into a tmpfs /tmp overflows it ([Errno 28]). Same-fs staging
    # also keeps the qemu-img convert single-device. (ESXI-31)
    with tempfile.TemporaryDirectory(prefix="tr_esxi_export_", dir=dest_path.parent) as tmp:
        local_desc = Path(tmp) / Path(ds_path).name
        local_flat = Path(tmp) / f"{local_desc.stem}-flat.vmdk"
        client.folder_get(descriptor_rel, local_desc)
        client.folder_get(flat_rel, local_flat)
        _diskconvert.vmdk_to_qcow2(local_desc, dest_path)
    return dest_path


def delete_volume(client: EsxiClient, vol_ref: VolumeRef) -> None:
    """Delete the disk (descriptor + extent) at the ref. Tolerant of absence."""
    ds_path = _ds_path(client, str(vol_ref))
    if not client.folder_exists(ds_path):
        _log.debug("delete_volume(%s): not present (already gone)", vol_ref)
        return
    if ds_path.endswith(".iso"):
        # A seed/boot ISO is a plain datastore file, not a managed disk.
        client.folder_delete(ds_path)
        return
    task = _vdm(client).DeleteVirtualDisk_Task(name=str(vol_ref), datacenter=_dc(client))
    client.wait_for_task(task)
    _log.info("deleted VMFS disk %s", vol_ref)
