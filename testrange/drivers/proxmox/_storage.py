"""Pool + volume I/O for the Proxmox backend (PVE-3).

The orchestrator works in one **stable** ``VolumeRef`` per disk, threaded through
``upload → (resize) → create_vm → download → delete``. PVE does not, and
reconciling that is the whole job of this module. Two realities shape it:

1. **A "pool" is not a PVE object.** The backing storage (``local``, a ``dir``
   store) is static config; a testrange pool is just a *filename-prefix
   namespace* inside it (``compose_volume_ref`` prefixes ``<pool>__``). So
   ``create_pool`` has nothing to allocate, and ``destroy_pool`` is a sweep of
   leftover content volumes carrying that prefix.

2. **The ref means different things on each side (Option-2).** ``upload`` and
   ``write`` create the *staging* content volume the ref literally names
   (``local:import/…`` or ``local:iso/…``). But the live disk a VM boots from
   and writes to is a *different*, vm-scoped volid PVE allocates inside
   ``create_vm`` (``local:<vmid>/vm-<vmid>-disk-<n>``). So:

   - ``download_from_pool(ref)`` must **re-resolve** the ref to that live disk
     (via :func:`_vm.resolve_disk` + the VM's config) — reading the ref's own
     staging file would hand back the stale *pre-boot* image, losing everything
     the build VM installed. This is the function to read carefully.
   - ``delete_volume(ref)`` deletes the *staging* volume (tolerant of absence —
     ``create_vm`` deletes the OS staging once it has imported it, and the
     vm-scoped disk is purged by ``destroy_vm``).

Transport split (driver is proxmoxer-only for the control plane): **volume bytes
move over SFTP both ways** — ``upload_to_pool``/``write_to_pool`` SFTP-*put*,
``download_from_pool`` SFTP-*get* — because PVE's REST has no volume byte-egress
and its ``upload`` endpoint 501s on large ``import`` disk images (PVE-23,
ADR-0008 §6). Both directions key on the same volid → on-host path mapping,
:func:`_naming.volid_relpath` under :meth:`ProxmoxClient.storage_path`; for a
``dir``/``nfs`` store, a file written under the content dir is discovered by
scan, so an SFTP-put produces the same volid the REST upload would have.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.base import VolumeRef
from testrange.drivers.proxmox import _naming, _vm
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.devices.pool.base import StoragePool
    from testrange.drivers.proxmox._client import ProxmoxClient

_log = get_logger(__name__)


def _host_path(client: ProxmoxClient, volid: str) -> str:
    """Absolute on-host path for a volid (storage root + content-relative path)."""
    return f"{client.storage_path().rstrip('/')}/{_naming.volid_relpath(volid)}"


# -- pools (a filename-prefix namespace, not a PVE object) -----------------


def create_pool(client: ProxmoxClient, pool: StoragePool, backend_name: str) -> str:
    """No backend allocation: the pool is the ``<backend_name>__`` filename prefix.

    We only confirm the backing storage exposes an on-host path (i.e. is a
    ``dir``/``nfs``-style store the SFTP transfers can reach); a block store
    would fail here rather than mid-transfer.
    """
    del pool
    client.storage_path()  # raises DriverError if the store has no path
    _log.info("pool %s ready (filename-prefix namespace on %s)", backend_name, client.storage)
    return f"pool:{backend_name}"


def destroy_pool(client: ProxmoxClient, backend_name: str) -> None:
    """Sweep any content volumes still carrying this pool's ``<backend_name>__`` prefix.

    A safety net: the normal flow deletes each volume explicitly. vm-scoped
    disks are not prefixed (they are purged with their VM), so this only catches
    leftover staging/iso content.
    """
    content = client.api.nodes(client.node).storage(client.storage).content.get()
    prefix = f"{backend_name}__"
    for vol in content:
        volid = vol["volid"]
        filename = volid.split("/", 1)[-1]
        if filename.startswith(prefix):
            _delete_content(client, volid)


# -- volume I/O ------------------------------------------------------------


def write_to_pool(client: ProxmoxClient, target_ref: VolumeRef, data: bytes) -> VolumeRef:
    """Write raw bytes as the content volume named by ``target_ref`` (replace-if-exists).

    Used for the cloud-init seed and sidecar-config ISOs. Staged to a temp file
    and SFTP-put into the storage's content dir (``template/iso/``) so PVE can
    attach it as a CDROM (PVE-23 — volume bytes go over SFTP, not the REST
    ``upload`` endpoint).
    """
    volid = str(target_ref)
    with tempfile.NamedTemporaryFile(prefix="tr_pve_write_", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        client.sftp_put(tmp_path, _host_path(client, volid))
    finally:
        tmp_path.unlink(missing_ok=True)
    return target_ref


def upload_to_pool(client: ProxmoxClient, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
    """Upload an orchestrator-host file to the staging content volume at ``target_ref``.

    This is the *import source* (base image for the OS disk; cached built disk
    for a run disk). ``create_vm`` later ``import-from``\\ s it into the VM's
    vm-scoped disk. SFTP-put into the storage's ``import/`` dir — PVE's REST
    ``upload`` endpoint 501s on large import images, and ``dir``/``nfs`` storage
    discovers the dropped file by scan, yielding the same ``…:import/<file>``
    volid (PVE-23, ADR-0008 §6). The ``import`` content type must be enabled on
    the storage (preflight checks).

    Idempotent per the ABC contract (PVE-25): if a volume already exists at the
    ref, return it without re-uploading — a retry or a crash-resume must not
    re-transfer a multi-GB image.
    """
    if _vm.content_volume_exists(client, str(target_ref)):
        _log.info("upload_to_pool: %s already staged; skipping re-upload", target_ref)
        return target_ref
    client.sftp_put(source_path, _host_path(client, str(target_ref)))
    return target_ref


def download_from_pool(client: ProxmoxClient, vol_ref: VolumeRef, dest_path: Path) -> Path:
    """Stream the **live** disk a ref now denotes back to the orchestrator host.

    The Option-2 re-resolution (see module docstring). ``vol_ref`` is the stable
    handle the orchestrator composed pre-create; the bytes worth capturing live
    in the vm-scoped disk PVE allocated at ``create_vm`` and the build VM then
    wrote to. We therefore:

    1. resolve ``vol_ref`` → ``(vmid, scsi_index)`` via the VM's stamped name
       (:func:`_vm.resolve_disk`);
    2. read that VM's config to get the exact live volid at ``scsi<index>``
       (robust against PVE's disk-number allocation — we never guess it);
    3. SFTP the volid's on-host file down.

    Reading ``vol_ref``'s own staging file instead would return the stale
    pre-boot image — the bug this whole dance exists to avoid.
    """
    vmid, scsi_index = _vm.resolve_disk(client, str(vol_ref))
    config = client.api.nodes(client.node).qemu(vmid).config.get()
    key = f"scsi{scsi_index}"
    entry = config.get(key)
    if not entry:
        raise DriverError(
            f"download_from_pool: VM {vmid} has no {key} (ref {vol_ref!r}); "
            "create_vm did not attach the expected disk"
        )
    live_volid = str(entry).split(",", 1)[0]  # strip ",size=…,…" options
    client.sftp_get(_host_path(client, live_volid), dest_path)
    return dest_path


def delete_volume(client: ProxmoxClient, vol_ref: VolumeRef) -> None:
    """Delete the *staging* content volume the ref names. Tolerant of absence.

    The vm-scoped disk a ref may currently denote is removed by ``destroy_vm``
    (purge), and ``create_vm`` already drops the OS staging once imported — so by
    the time the orchestrator calls this, the named volume is often already
    gone. That is success, not an error.

    Absence is established by a positive existence check, not by swallowing the
    delete's failure (PVE-26): a permission error, an in-use volume, or an API
    outage must surface — otherwise teardown would ``forget`` the resource and
    leak it. So: gone → no-op; present → delete and let any error propagate.
    """
    if not _vm.content_volume_exists(client, str(vol_ref)):
        _log.debug("delete_volume(%s): not present (already gone)", vol_ref)
        return
    _delete_content(client, str(vol_ref))


def _delete_content(client: ProxmoxClient, volid: str) -> None:
    """Delete a content volume the caller has already confirmed exists.

    Errors propagate (the caller has established presence, so a failure here is a
    real one — in-use, permissions, API outage — not a benign absence).
    """
    result = client.api.nodes(client.node).storage(client.storage).content(volid).delete()
    if isinstance(result, str) and result.startswith("UPID:"):
        client.wait_task(result)


# -- deferred-to-create_vm sizing (PVE-8 owns the realisation) -------------


def create_blank_volume(target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """No-op: blank data disks are allocated inside ``create_vm``, sized from spec.

    PVE allocates a disk only against a vmid, which does not exist until
    ``create_vm``. Rather than carry cross-call state, ``create_vm`` allocates
    each blank data disk directly (``scsi<i+1>=<storage>:<size_gb>``) using the
    sizes already on the ``VMSpec`` it receives. The orchestrator's
    ``create_blank_volume`` → ``create_vm`` ordering and end-state (the VM boots
    with a sized blank disk) are preserved; only the realisation point moves.
    """
    del size_gb
    return target_ref


def resize_volume(target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """No-op: the OS disk is grown inside ``create_vm`` after ``import-from``.

    Same rationale as :func:`create_blank_volume` — there is no vm-scoped disk to
    resize until ``create_vm``, which grows ``scsi0`` to ``spec.os_drive.size_gb``
    right after importing the base. The pre-create ``resize_volume(ref, size)``
    call records intent the orchestrator already holds on the spec.
    """
    del size_gb
    return target_ref
