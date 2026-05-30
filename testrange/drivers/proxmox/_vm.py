"""VM identity resolution for the Proxmox backend (PVE-6).

Why this module exists — the name→vmid problem
-----------------------------------------------
PVE addresses a guest by a numeric **vmid** allocated at create time
(``/cluster/nextid``), but the orchestrator only ever knows the deterministic
**backend name** it composed (``compose_resource_name`` → ``tr-vm-<run>-web``).
Every later operation the orchestrator drives — ``start_vm``, ``shutdown_vm``,
``destroy_vm``, ``get_vm_power_state``, snapshots, and the Option-2 disk
resolution in ``download_from_pool`` — is keyed on that backend name and must
recover the vmid.

Per ADR-0008 §6 we keep **no external map**: ``create_vm`` stamps the composed
name into the VM's PVE ``name`` field, and resolution scans the node's guest
list for it. A teardown driver rebuilt from the state-file URI (which never saw
the ``run_id`` or any vmid) therefore recovers the handle from the backend
alone — crash-safe cleanup with nothing to lose.

These functions take the live :class:`ProxmoxClient` and are exercised in unit
tests via a duck-typed fake; the VM *lifecycle* (create/start/stop/...) lands in
PVE-8 and will live alongside this.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from testrange._log import get_logger
from testrange.drivers.proxmox import _naming
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

    from testrange.drivers.base import VolumeRef
    from testrange.drivers.proxmox._client import ProxmoxClient
    from testrange.networks.base import BuildNic
    from testrange.vms.spec import VMSpec

_log = get_logger(__name__)

# Poll cadence for the post-create config-lock wait (import-from/resize).
_POLL_INTERVAL_S = 1.0


def list_vms(client: ProxmoxClient) -> dict[str, int]:
    """Map ``{stamped backend name: vmid}`` for every guest on the node.

    Guests with no ``name`` (never created by testrange) are skipped. The PVE
    ``name`` is exactly the backend name ``create_vm`` stamped, so this is the
    inverse of the stamping.
    """
    return {
        v["name"]: int(v["vmid"]) for v in client.api.nodes(client.node).qemu.get() if v.get("name")
    }


def resolve_vmid(client: ProxmoxClient, backend_name: str) -> int:
    """The vmid of the VM stamped ``backend_name``. Raises if absent.

    Used by every vmid-keyed lifecycle/snapshot call. A miss is a hard error
    (the VM the orchestrator believes it created is gone) rather than a silent
    skip, so teardown surfaces drift instead of leaking.
    """
    vmid = list_vms(client).get(backend_name)
    if vmid is None:
        raise DriverError(
            f"no PVE VM named {backend_name!r} on node {client.node!r} "
            f"(stamped-name resolution found none)"
        )
    return vmid


def resolve_disk(client: ProxmoxClient, disk_ref: str) -> tuple[int, int]:
    """Resolve a disk ``VolumeRef`` to its live ``(vmid, scsi_index)``.

    The backbone of the "Option-2" disk model. The orchestrator threads one
    stable ref through ``upload → create_vm → download → delete``, but PVE
    realises the live disk as a *different* vm-scoped volid
    (``local:<vmid>/vm-<vmid>-disk-<n>``) allocated inside ``create_vm`` — so a
    naive ``download_from_pool(ref)`` would read the stale pre-boot upload, not
    what the VM wrote. We re-resolve the ref to the live disk instead of holding
    in-process state:

    1. ``ref`` → ``vol_name`` (carries the owning VM's backend name + role).
    2. Match ``vol_name`` against the node's stamped VM names; the **longest**
       matching name is the owner (disambiguates a VM whose name happens to be a
       prefix of another's, e.g. ``web`` vs ``web-data0``).
    3. The role (OS vs ``data<i>``) gives the ``scsiN`` index ``create_vm`` used.

    The caller reads the VM's config ``scsi<index>`` to get the exact live
    volid. Raises :class:`DriverError` if no stamped VM owns the ref.
    """
    _pool, vol_name = _naming.parse_disk_ref(disk_ref)
    owner: tuple[int, int] | None = None
    owner_name = ""
    for name, vmid in list_vms(client).items():
        idx = _naming.disk_scsi_index(vol_name, name)
        if idx is not None and len(name) > len(owner_name):
            owner, owner_name = (vmid, idx), name
    if owner is None:
        raise DriverError(
            f"no PVE VM owns disk ref {disk_ref!r} (vol_name {vol_name!r}); "
            "its VM was never created or has been destroyed"
        )
    return owner


# -- lifecycle (PVE-8) -----------------------------------------------------


def _await(client: ProxmoxClient, result: Any) -> None:
    """Block on a PVE task if the call returned a UPID; no-op otherwise."""
    if isinstance(result, str) and result.startswith("UPID:"):
        client.wait_task(result)


def content_volume_exists(client: ProxmoxClient, volid: str) -> bool:
    """Whether a content volume with ``volid`` currently exists on the storage.

    Lists the storage's content and tests membership rather than probing the
    single-volid endpoint and treating *any* error as absence: a transient API
    or permission error must propagate as itself, not be silently misread as
    "not there" (PVE-26) — which would turn a run-phase cached disk into a blank
    or drop tracking of a volume that actually leaked. Shared by ``create_vm``
    (run-phase cached disk vs build blank) and ``_storage`` (idempotent upload /
    absence-tolerant delete).
    """
    content = client.api.nodes(client.node).storage(client.storage).content.get()
    return any(v.get("volid") == volid for v in content)


def create_vm(
    client: ProxmoxClient,
    backend_name: str,
    spec: VMSpec,
    plan_name: str,
    *,
    os_disk_ref: VolumeRef,
    seed_iso_ref: VolumeRef | None,
    network_refs: dict[str, str],
    data_disk_refs: Sequence[VolumeRef] = (),
    build_nic: BuildNic | None = None,
) -> str:
    """Define a VM on PVE from the orchestrator's staged disks.

    All proxmoxer; no ``qemu-img``/subprocess (disk sizing is REST-native). The
    composed ``backend_name`` is stamped into the PVE ``name`` so :func:`resolve_vmid`
    recovers the vmid later (ADR-0008 §6). Disk realisation (the "Option-2"
    crux):

    - **OS disk** — always ``import-from`` the uploaded staging volume
      (``os_disk_ref``) into a fresh vm-scoped ``scsi0``. When a seed is present
      (build VM / sidecar — a *small base* was imported) the disk is then grown
      to ``spec.os_drive.size_gb`` so cloud-init's ``growpart`` can expand the
      rootfs. A seed-less VM (run phase) imported an already-full-size cached
      disk, so it is **not** resized (PVE rejects a resize to the same size).
    - **Data disks** — run-phase refs were uploaded as staging → ``import-from``;
      build-phase refs are blanks (``create_blank_volume`` is a no-op) → allocate
      a fresh sized disk (``<storage>:<size_gb>``) from the spec.
    - **Seed ISO** (cloud-init seed or sidecar config) — attached as an ``ide2``
      CDROM; boot order pinned to ``scsi0`` since the seed is data, not bootable.
    - **NICs** — ``net<i>`` for each ``spec.nics[i]`` on its backend bridge/vnet
      (``network_refs``) with the stable MAC ``compose_mac(plan, vm, i)`` so DHCP
      hands out a predictable lease (ADR-0006). At build (``build_nic`` set,
      ADR-0017) the declared NICs are replaced by a single ``net0`` on the build
      network carrying the build NIC's MAC.
    """
    storage = client.storage
    vmid = int(client.api.cluster.nextid.get())
    config: dict[str, Any] = {
        "vmid": vmid,
        "name": backend_name,
        "cores": spec.cpu.count,
        "memory": spec.memory.size_mb,
        "scsihw": "virtio-scsi-single",
        "ostype": "l26",
        "agent": 1,  # QEMU Guest Agent (the PVE-4 native transport rides this)
        "serial0": "socket",  # cloud images expect a serial console
        "boot": "order=scsi0",  # the seed ISO is data, not bootable
        "scsi0": f"{storage}:0,import-from={os_disk_ref}",
    }
    # Build-vs-run for data disks follows the orchestrator's *intent*, not a
    # backend probe (PVE-27). A build/sidecar create carries a cloud-init/config
    # seed and attaches every writable disk as a BLANK for the guest to populate
    # (ADR-0010 §4); a run create has ``seed_iso_ref=None`` and IMPORTs each disk
    # from the cached staging volume run_phase already uploaded. This is the same
    # seed-presence signal the OS-disk grow below keys on, so the two never
    # disagree — and it can't be fooled by a stale staging file left behind by a
    # crashed prior build (which the old "does the volume exist?" probe would have
    # mis-imported). NB: when installer-based OS-disk origins land (BUILD-1), a
    # build may carry no cloud-init seed and this discriminator must be revisited.
    is_build = seed_iso_ref is not None
    for i, ref in enumerate(data_disk_refs):
        if is_build:
            config[f"scsi{i + 1}"] = f"{storage}:{spec.data_drives[i].size_gb}"  # blank
        else:
            config[f"scsi{i + 1}"] = f"{storage}:0,import-from={ref}"  # run: cached built disk
    if seed_iso_ref is not None:
        config["ide2"] = f"{seed_iso_ref},media=cdrom"
    if build_nic is not None:
        # Build phase (ADR-0017): one build NIC, declared NICs not attached.
        config["net0"] = f"virtio={build_nic.mac},bridge={network_refs[build_nic.network]}"
    else:
        for idx, nic in enumerate(spec.nics):
            mac = _naming.compose_mac(plan_name, spec.name, idx)
            config[f"net{idx}"] = f"virtio={mac},bridge={network_refs[nic.network]}"

    _await(client, client.api.nodes(client.node).qemu.post(**config))
    _wait_unlocked(client, vmid)
    if seed_iso_ref is not None:
        _resize_os_disk(client, vmid, spec.os_drive.size_gb)
        _wait_unlocked(client, vmid)
    _log.info("created PVE vm %s (vmid %d)", backend_name, vmid)
    return f"vm:{vmid}"


def _wait_unlocked(client: ProxmoxClient, vmid: int, *, timeout: float = 300.0) -> None:
    """Block until the VM config carries no ``lock`` (the disk-import/resize task is done)."""
    start = time.monotonic()
    while True:
        lock = client.api.nodes(client.node).qemu(vmid).config.get().get("lock")
        if not lock:
            return
        if time.monotonic() - start > timeout:
            raise DriverError(f"vm {vmid} still config-locked ({lock!r}) after {timeout:.0f}s")
        time.sleep(_POLL_INTERVAL_S)


def _resize_os_disk(
    client: ProxmoxClient, vmid: int, size_gb: int, *, attempts: int = 8, backoff_s: float = 4.0
) -> None:
    """Grow ``scsi0`` to ``size_gb``, retrying the transient post-import lock race.

    Even after the import-from task completes and the config lock clears,
    ``qemu-img`` briefly cannot acquire the freshly-imported image's file lock,
    so the resize task fails with "got timeout" (validated against the live host:
    it clears within a few seconds). Since this is the *task* failing (not a
    config lock), we retry the resize itself with a short backoff.
    """
    for attempt in range(1, attempts + 1):
        try:
            _await(
                client,
                client.api.nodes(client.node)
                .qemu(vmid)
                .resize.put(disk="scsi0", size=f"{size_gb}G"),
            )
            return
        except DriverError as e:
            msg = str(e).lower()
            transient = "timeout" in msg or "lock" in msg
            if attempt == attempts or not transient:
                raise
            _log.info("vmid %d resize attempt %d hit a transient lock; retrying", vmid, attempt)
            time.sleep(backoff_s)


def start_vm(client: ProxmoxClient, backend_name: str) -> None:
    vmid = resolve_vmid(client, backend_name)
    _await(client, client.api.nodes(client.node).qemu(vmid).status.start.post())


def shutdown_vm(client: ProxmoxClient, backend_name: str, *, timeout: float = 120.0) -> None:
    """Graceful ACPI shutdown, hard-stopping after ``timeout`` (``forceStop``)."""
    vmid = resolve_vmid(client, backend_name)
    result = (
        client.api.nodes(client.node)
        .qemu(vmid)
        .status.shutdown.post(timeout=int(timeout), forceStop=1)
    )
    _await_with_margin(client, result, timeout)


def destroy_vm(client: ProxmoxClient, backend_name: str) -> None:
    """Stop (if needed) then purge the VM and its disks.

    ``purge=1`` + ``destroy-unreferenced-disks=1`` removes the vm-scoped disks
    (so a separate ``delete_volume`` on a disk ref is a tolerant no-op).
    """
    vmid = resolve_vmid(client, backend_name)
    try:
        _await(client, client.api.nodes(client.node).qemu(vmid).status.stop.post())
    except Exception as e:
        _log.debug("destroy_vm: stop %d failed (likely already stopped): %s", vmid, e)
    _await(
        client,
        client.api.nodes(client.node)
        .qemu(vmid)
        .delete(purge=1, **{"destroy-unreferenced-disks": 1}),
    )
    _log.info("destroyed PVE vm %s (vmid %d)", backend_name, vmid)


def get_vm_power_state(client: ProxmoxClient, backend_name: str) -> str:
    """The VM's power state in the orchestrator's vocabulary (``shutoff``/``running``).

    PVE reports ``stopped``; the orchestrator compares against ``shutoff``
    (the cross-backend term), so map it.
    """
    vmid = resolve_vmid(client, backend_name)
    status = client.api.nodes(client.node).qemu(vmid).status.current.get()["status"]
    return "shutoff" if status == "stopped" else str(status)


def _await_with_margin(client: ProxmoxClient, result: Any, timeout: float) -> None:
    if isinstance(result, str) and result.startswith("UPID:"):
        client.wait_task(result, timeout=timeout + 30.0)


# -- snapshots (PVE-5) -----------------------------------------------------


def _snapshot_names(client: ProxmoxClient, vmid: int) -> list[str]:
    """Snapshot names on a VM, oldest-first, excluding PVE's synthetic ``current``.

    PVE injects a ``current`` pseudo-entry (the live state) into the snapshot
    list; it is not a real snapshot, so we drop it. Ordering is by ``snaptime``.
    """
    snaps = [
        s
        for s in client.api.nodes(client.node).qemu(vmid).snapshot.get()
        if s.get("name") != "current"
    ]
    snaps.sort(key=lambda s: s.get("snaptime", 0))
    return [s["name"] for s in snaps]


def create_snapshot(
    client: ProxmoxClient,
    vm_backend_name: str,
    name: str,
    description: str = "",
    *,
    mem: bool = False,
) -> None:
    """Snapshot the VM. ``mem=True`` includes RAM state (``vmstate=1``).

    A memory snapshot requires the VM to be running (PVE enforces this). Raises
    :class:`DriverError` if ``name`` already exists, per the ABC contract.
    """
    vmid = resolve_vmid(client, vm_backend_name)
    if name in _snapshot_names(client, vmid):
        raise DriverError(f"snapshot {name!r} already exists on vm {vm_backend_name!r}")
    _await(
        client,
        client.api.nodes(client.node)
        .qemu(vmid)
        .snapshot.post(snapname=name, description=description, vmstate=1 if mem else 0),
    )
    _log.info("created snapshot %s on vm %s (mem=%s)", name, vm_backend_name, mem)


def list_snapshots(client: ProxmoxClient, vm_backend_name: str) -> list[str]:
    return _snapshot_names(client, resolve_vmid(client, vm_backend_name))


def delete_snapshot(client: ProxmoxClient, vm_backend_name: str, name: str) -> None:
    """Delete a snapshot. No-op if ``name`` doesn't exist (per the ABC)."""
    vmid = resolve_vmid(client, vm_backend_name)
    if name not in _snapshot_names(client, vmid):
        return
    _await(client, client.api.nodes(client.node).qemu(vmid).snapshot(name).delete())
    _log.info("deleted snapshot %s on vm %s", name, vm_backend_name)


def restore_snapshot(client: ProxmoxClient, vm_backend_name: str, name: str) -> None:
    """Roll the VM back to ``name``. Raises :class:`DriverError` if it's absent."""
    vmid = resolve_vmid(client, vm_backend_name)
    if name not in _snapshot_names(client, vmid):
        raise DriverError(f"snapshot {name!r} not found on vm {vm_backend_name!r}")
    _await(client, client.api.nodes(client.node).qemu(vmid).snapshot(name).rollback.post())
    _log.info("rolled vm %s back to snapshot %s", vm_backend_name, name)
