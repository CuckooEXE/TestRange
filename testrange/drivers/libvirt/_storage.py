"""Pool + volume I/O for the libvirt backend (BACKEND-1.A).

libvirt has first-class storage objects, so this is far simpler than the Proxmox
"Option-2" dance: a testrange pool is a real per-run **dir** storage pool the
driver defines/builds/creates under ``/var/lib/libvirt/images/tr-pool-<run8>-<pool>``
(``pool.build()`` mkdirs it *as the daemon*, so no root on the runner), and a
volume is a real ``virStorageVol`` addressed as ``<pool>/<name>``. Volume bytes
move over the libvirt **stream API** (``virStorageVol.upload``/``download``) in
both directions — no ``qemu-img``, no subprocess. Disks are qcow2 throughout and
always full-content (no backing chains), so ``download_from_pool``'s
self-contained invariant holds for free.

The functions take the live :class:`LibvirtClient` and are exercised in unit
tests via a duck-typed fake; live validation rides the integration suite.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers.base import VolumeRef
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from testrange.devices.pool.base import StoragePool
    from testrange.drivers.libvirt._conn import LibvirtClient

_log = get_logger(__name__)

_GiB = 1024**3
# Stream pump chunk. libvirt's sendAll/recvAll wrappers ask the handler for a
# bounded slice; 4 MiB keeps the per-call count low on a multi-hundred-MiB image
# without holding the whole file in memory.
_CHUNK = 4 * 1024 * 1024


def _split_ref(ref: VolumeRef) -> tuple[str, str]:
    """``<pool>/<vol_name>`` → ``(pool, vol_name)``. Raises on a malformed ref."""
    pool_name, sep, vol_name = str(ref).partition("/")
    if not sep or not pool_name or not vol_name:
        raise DriverError(f"malformed libvirt VolumeRef {ref!r} (expected '<pool>/<name>')")
    return pool_name, vol_name


def _vol_format(vol_name: str) -> str:
    """The on-disk format for a volume, from its name suffix.

    Seeds/config are ``.iso`` (raw); every disk is ``.qcow2``. Defaults to qcow2
    so an unsuffixed name still produces a valid disk.
    """
    return "raw" if vol_name.endswith(".iso") else "qcow2"


def _volume_xml(vol_name: str, *, capacity_bytes: int, fmt: str) -> str:
    # allocation is omitted (sparse): a blank qcow2 data disk and an
    # about-to-be-overwritten upload target both want a thin file.
    return (
        "<volume>"
        f"<name>{vol_name}</name>"
        f"<capacity unit='bytes'>{max(capacity_bytes, 1)}</capacity>"
        f"<target><format type='{fmt}'/></target>"
        "</volume>"
    )


def _pool_xml(backend_name: str, path: str) -> str:
    return (
        f"<pool type='dir'><name>{backend_name}</name><target><path>{path}</path></target></pool>"
    )


def create_pool(client: LibvirtClient, pool: StoragePool, backend_name: str) -> str:
    """Define → build → create a per-run dir pool under /var/lib/libvirt/images.

    ``build`` mkdirs the target *as the daemon* (root-owned parent), so the
    runner needs no root — just ``libvirt`` group membership. ``pool.size_gb`` is
    a capacity precondition, not a quota a dir pool imposes; it is not enforced
    here.
    """
    del pool
    path = f"/var/lib/libvirt/images/{backend_name}"
    sp = client.raw.storagePoolDefineXML(_pool_xml(backend_name, path), 0)
    sp.build(0)
    sp.create(0)
    _log.info("created libvirt dir pool %s at %s", backend_name, path)
    return f"pool:{backend_name}"


def destroy_pool(client: LibvirtClient, backend_name: str) -> None:
    """Stop (if active) → delete the backing dir → undefine. Tolerant of absence.

    A pool the orchestrator never created (or already removed) is a no-op, not an
    error; any *other* failure propagates so teardown surfaces drift.
    """
    pool = client.lookup_pool(backend_name)
    if pool is None:
        _log.debug("destroy_pool(%s): not present (already gone)", backend_name)
        return
    # Sweep any leftover volumes first: a dir pool's delete() removes the backing
    # directory only when it is empty. The normal flow deletes each volume
    # explicitly (state-driven cleanup), so this just guards a partial/crashed run
    # from leaking the run directory.
    for vol in pool.listAllVolumes(0):
        vol.delete(0)
    if pool.isActive():
        pool.destroy()
    pool.delete(0)
    pool.undefine()
    _log.info("destroyed libvirt pool %s", backend_name)


def _replace_volume(client: LibvirtClient, ref: VolumeRef, *, capacity_bytes: int) -> Any:
    """Delete any volume already at ``ref`` and create a fresh one (replace-if-exists)."""
    pool_name, vol_name = _split_ref(ref)
    pool = client.lookup_pool(pool_name)
    if pool is None:
        raise DriverError(f"create volume {ref!r}: pool {pool_name!r} does not exist")
    existing = client.lookup_volume(pool_name, vol_name)
    if existing is not None:
        existing.delete(0)
    return pool.createXML(
        _volume_xml(vol_name, capacity_bytes=capacity_bytes, fmt=_vol_format(vol_name)), 0
    )


def create_blank_volume(client: LibvirtClient, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """Provision a blank, sized qcow2 volume (data disks; replace-if-exists)."""
    _replace_volume(client, target_ref, capacity_bytes=size_gb * _GiB)
    _log.info("created blank volume %s (%d GiB)", target_ref, size_gb)
    return target_ref


def write_to_pool(client: LibvirtClient, target_ref: VolumeRef, data: bytes) -> VolumeRef:
    """Write raw bytes as a new volume (seed / sidecar-config ISO; replace-if-exists)."""
    vol = _replace_volume(client, target_ref, capacity_bytes=len(data))
    _stream_in(client, vol, io.BytesIO(data), length=len(data))
    return target_ref


def upload_to_pool(client: LibvirtClient, target_ref: VolumeRef, source_path: Path) -> VolumeRef:
    """Stream an orchestrator-host file into the pool at ``target_ref``.

    Idempotent per the ABC: an existing volume at the ref is returned untouched
    (a retry / crash-resume must not re-transfer a multi-hundred-MiB image). The
    uploaded bytes are the qcow2 file verbatim; ``pool.refresh`` afterwards makes
    libvirt re-read the real virtual capacity from the qcow2 header (the create
    capacity was only the file size), so a later ``resize_volume`` compares
    against the true size.
    """
    pool_name, vol_name = _split_ref(target_ref)
    if client.lookup_volume(pool_name, vol_name) is not None:
        _log.info("upload_to_pool: %s already present; skipping re-upload", target_ref)
        return target_ref
    size = source_path.stat().st_size
    vol = _replace_volume(client, target_ref, capacity_bytes=size)
    with source_path.open("rb") as fh:
        _stream_in(client, vol, fh, length=size)
    pool = client.lookup_pool(pool_name)
    if pool is not None:
        pool.refresh(0)
    _log.info("uploaded %s (%d bytes) to %s", source_path.name, size, target_ref)
    return target_ref


def resize_volume(client: LibvirtClient, target_ref: VolumeRef, size_gb: int) -> VolumeRef:
    """Grow the volume's virtual capacity to ``size_gb`` (no qemu-img).

    Used to expand an image-based OS disk before the build boot so cloud-init's
    ``growpart`` can claim the space. Grow-only: ``vol.resize`` rejects a target
    below current capacity, which is exactly the ABC's no-shrink contract.
    """
    pool_name, vol_name = _split_ref(target_ref)
    vol = client.lookup_volume(pool_name, vol_name)
    if vol is None:
        raise DriverError(f"resize_volume: no volume at {target_ref!r}")
    vol.resize(size_gb * _GiB, 0)
    _log.info("resized %s to %d GiB", target_ref, size_gb)
    return target_ref


def download_from_pool(client: LibvirtClient, vol_ref: VolumeRef, dest_path: Path) -> Path:
    """Stream a pool volume's bytes (the full qcow2 file) to a host path.

    Symmetric inverse of ``upload_to_pool``; the volume is self-contained (no
    backing chain), so the captured file is a complete disk.
    """
    pool_name, vol_name = _split_ref(vol_ref)
    vol = client.lookup_volume(pool_name, vol_name)
    if vol is None:
        raise DriverError(f"download_from_pool: no volume at {vol_ref!r}")
    with dest_path.open("wb") as fh:
        _stream_out(client, vol, fh)
    return dest_path


def delete_volume(client: LibvirtClient, vol_ref: VolumeRef) -> None:
    """Delete the volume at ``vol_ref``. Tolerant of absence (already gone)."""
    pool_name, vol_name = _split_ref(vol_ref)
    vol = client.lookup_volume(pool_name, vol_name)
    if vol is None:
        _log.debug("delete_volume(%s): not present (already gone)", vol_ref)
        return
    vol.delete(0)


def _stream_in(
    client: LibvirtClient, vol: Any, source: io.BufferedReader | io.BytesIO, *, length: int
) -> None:
    """Upload ``length`` bytes from a binary file-like into ``vol`` over a stream."""
    stream = client.raw.newStream(0)
    vol.upload(stream, 0, length, 0)

    def _reader(_stream: Any, nbytes: int, _opaque: Any) -> bytes:
        return source.read(min(nbytes, _CHUNK))

    try:
        stream.sendAll(_reader, None)
        stream.finish()
    except BaseException:
        stream.abort()
        raise


def _stream_out(client: LibvirtClient, vol: Any, dest: io.BufferedWriter) -> None:
    """Download the whole volume into a binary file-like over a stream."""
    stream = client.raw.newStream(0)
    vol.download(stream, 0, 0, 0)

    def _writer(_stream: Any, data: bytes, _opaque: Any) -> int:
        dest.write(data)
        return len(data)

    try:
        stream.recvAll(_writer, None)
        stream.finish()
    except BaseException:
        stream.abort()
        raise
