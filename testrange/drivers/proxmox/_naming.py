"""Deterministic naming for the Proxmox backend.

Pure functions only — same inputs, same outputs (the cleanup walker rebuilds
refs without any live state). Two PVE charset realities drive the work here:

- a VM ``name`` is a DNS label (``[a-z0-9-]``, no ``_``/``.``), and
- an SDN vnet id is ``<= 8`` alphanumeric chars starting with a letter.

so the composed orchestrator names are sanitised down to those shapes.
"""

from __future__ import annotations

import hashlib
import re

from testrange.drivers.base import VolumeRef

# Locally-administered, unicast OUI (bit 0x02 of the first octet set). Stable
# MACs let DHCP hand out the same lease across runs (ADR-0006).
_OUI_FIRST = 0x02

_SUFFIXES = {
    "build_disk": ".qcow2",
    "run_disk": ".qcow2",
    "data_disk": ".qcow2",
    "base_image": ".qcow2",
    "build_seed": ".iso",
    "sidecar_disk": ".qcow2",
    "sidecar_config": ".iso",
}

# A VM name is a DNS label; everything else collapses to a hyphen.
_NOT_DNS = re.compile(r"[^a-z0-9-]+")
# Storage filenames tolerate a wider set; only path-hostile chars are dropped.
_NOT_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_PVE_NAME_MAX = 60


def _pve_name(value: str) -> str:
    """Sanitise an arbitrary string into a PVE DNS-label-safe name.

    Deterministic and collision-resistant: if sanitisation/truncation would
    lose information, a short hash of the original is appended so distinct
    inputs stay distinct.
    """
    cleaned = _NOT_DNS.sub("-", value.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned) or "x"
    if cleaned != value.lower() or len(cleaned) > _PVE_NAME_MAX:
        suffix = hashlib.sha256(value.encode()).hexdigest()[:6]
        cleaned = f"{cleaned[: _PVE_NAME_MAX - 7]}-{suffix}"
    return cleaned


def compose_resource_name(run_id: str, kind: str, name: str) -> str:
    """Deterministic backend name, sanitised to a PVE DNS label.

    The VM variant is also the name→vmid recovery anchor (ADR-0008 #6): the
    driver stamps it into the VM ``name`` and resolves it back on teardown.
    """
    return _pve_name(f"tr-{kind}-{run_id[:8]}-{name}")


def compose_mac(plan_name: str, vm_name: str, nic_idx: int) -> str:
    digest = hashlib.sha256(f"{plan_name}/{vm_name}/{nic_idx}".encode()).digest()
    octets = [_OUI_FIRST, *digest[:5]]
    return ":".join(f"{b:02x}" for b in octets)


def compose_volume_ref(storage: str, pool_backend_name: str, vol_name: str) -> VolumeRef:
    """Pure ``VolumeRef`` keyed on ``(storage, pool, vol_name)``.

    Seeds (``.iso``) and built/base images (``.qcow2`` reaching here via
    ``upload_to_pool``) are real content volumes — the ref is their actual PVE
    volid (``local:iso/...`` / ``local:import/...``). Disk refs (``.qcow2``
    pushed via ``upload_to_pool`` or sized via ``create_blank_volume``) reuse
    the same shape as an opaque handle; the real disk is the vm-scoped volid PVE
    allocates at ``create_vm`` via ``import-from``, so a disk ref is never
    realised as a content volume.
    """
    content = "iso" if vol_name.endswith(".iso") else "import"
    filename = _NOT_FILENAME.sub("-", f"{pool_backend_name}__{vol_name}")
    return VolumeRef(f"{storage}:{content}/{filename}")


def volume_suffix(kind: str) -> str:
    return _SUFFIXES[kind]


def vnet_id(backend_name: str) -> str:
    """Stable 8-char SDN vnet id (alnum, leading letter) for a switch backend name."""
    return "v" + hashlib.sha1(backend_name.encode(), usedforsecurity=False).hexdigest()[:7]


def volid_storage(ref: str) -> str:
    """The storage id portion of a volid (the part before the first ``:``)."""
    return ref.split(":", 1)[0]


def is_iso_ref(ref: str) -> bool:
    return ":iso/" in ref


def parse_disk_ref(ref: str) -> tuple[str, str]:
    """Recover ``(pool_backend, vol_name)`` from a content/disk ``VolumeRef``.

    Inverse of :func:`compose_volume_ref`'s filename composition. A ref is
    ``<storage>:<content>/<pool_backend>__<vol_name>`` (e.g.
    ``local:import/tr-pool-ab12cd-p1__tr-build-ab12cd-web.qcow2``). Both
    ``pool_backend`` and the VM backend name embedded in ``vol_name`` are PVE DNS
    labels (no ``_``), so the ``__`` separator splits cleanly on its first hit.
    """
    filename = ref.split(":", 1)[1].split("/", 1)[1]
    pool_backend, vol_name = filename.split("__", 1)
    return pool_backend, vol_name


def volid_filename(volid: str) -> str:
    """The bare filename of a volid (``local:import/p__web.qcow2`` → ``p__web.qcow2``).

    The name PVE should store the uploaded file under, so the resulting volid
    equals the ref the orchestrator composed.
    """
    return volid.split("/", 1)[1]


def volid_relpath(volid: str) -> str:
    """Filesystem path of a volid *relative to the storage root*.

    Maps PVE's content-type prefixes to the on-disk layout of a ``dir`` storage
    so the SFTP transfers can locate the file under ``storage_path()``:

    - ``local:iso/x.iso``              → ``template/iso/x.iso``
    - ``local:import/x.qcow2``         → ``import/x.qcow2``
    - ``local:107/vm-107-disk-0.qcow2``→ ``images/107/vm-107-disk-0.qcow2`` (vm-scoped)
    """
    rest = volid.split(":", 1)[1]
    head, _, filename = rest.partition("/")
    if head == "iso":
        return f"template/iso/{filename}"
    if head == "import":
        return f"import/{filename}"
    if head.isdigit():  # vm-scoped disk: <vmid>/<file>
        return f"images/{head}/{filename}"
    raise ValueError(f"volid_relpath: unrecognised volid shape {volid!r}")


def disk_scsi_index(vol_name: str, vm_backend_name: str) -> int | None:
    """The ``scsiN`` index ``vol_name`` maps to on ``vm_backend_name``, or ``None``.

    The orchestrator names a VM's OS disk ``<vm_backend>.<ext>`` and its i-th
    data disk ``<vm_backend>-data<i>.<ext>`` (see ``build_phase``/``run_phase``);
    :meth:`ProxmoxDriver.create_vm` attaches the OS disk at ``scsi0`` and data
    disk ``i`` at ``scsi<i+1>``. This recovers that index so
    ``download_from_pool`` can find the live disk a stable ref now denotes.
    Returns ``None`` when ``vol_name`` is not a disk of ``vm_backend_name``.
    """
    base = vol_name.rsplit(".", 1)[0]
    if base == vm_backend_name:
        return 0
    m = re.fullmatch(re.escape(vm_backend_name) + r"-data(\d+)", base)
    return int(m.group(1)) + 1 if m else None
