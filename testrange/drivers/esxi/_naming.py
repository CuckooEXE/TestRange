"""Deterministic naming for the ESXi backend (ESXI-7).

Pure functions only — same inputs, same outputs (the cleanup walker rebuilds
refs without any live state). Two ESXi realities drive the work here:

- An inventory **VM name** becomes a datastore **folder name** (``[ds] <name>/``)
  and must avoid filesystem-hostile characters; ESXi caps it at ~80 chars.
- A manually-assigned **MAC** must fall in VMware's manual range
  ``00:50:56:00:00:00`` to ``00:50:56:3f:ff:ff`` -- the host rejects a ``manual``
  NIC MAC outside it (this is *why* ESXi's ``compose_mac`` differs from the
  locally-administered ``0x02`` OUI the libvirt/Proxmox backends use).

Volume refs use ESXi's datastore-path bracket form ``[<datastore>] <pool>/<name>``
— the exact shape pyVmomi expects as a disk backing ``fileName`` and CDROM ISO
path. The canonical cache name is ``.qcow2`` (decision A); a *disk* ref maps that
to the derived on-datastore ``.vmdk`` (never content-addressed — qemu-img vmdk
output is not byte-deterministic), while a seed ``.iso`` is carried through.
"""

from __future__ import annotations

import hashlib
import re

from testrange.drivers.base import VolumeRef

# VMware manual-assignment OUI + the 4th-octet ceiling (0x00..0x3f) of the
# manual MAC range. Outside this the host refuses a `manual` NIC address.
_VMWARE_OUI = (0x00, 0x50, 0x56)
_MANUAL_4TH_MASK = 0x3F

_SUFFIXES = {
    "build_disk": ".qcow2",
    "run_disk": ".qcow2",
    "data_disk": ".qcow2",
    "base_image": ".qcow2",
    "build_seed": ".iso",
    "boot_iso": ".iso",
    "sidecar_disk": ".qcow2",
    "sidecar_config": ".iso",
}

# Inventory/folder names: keep a conservative filesystem-safe set; collapse the
# rest to a hyphen and append a short hash when sanitisation loses information.
_NOT_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_HASH_SUFFIX_LEN = 6
_ESXI_NAME_MAX = 78  # ESXi caps inventory names ~80; leave margin


def _esxi_name(value: str) -> str:
    """Sanitise an arbitrary string into an ESXi-safe inventory/folder name.

    Deterministic and collision-resistant: if sanitisation or truncation would
    lose information, a short hash of the original is appended so distinct
    inputs stay distinct.
    """
    cleaned = _NOT_SAFE.sub("-", value).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned) or "x"
    if cleaned != value or len(cleaned) > _ESXI_NAME_MAX:
        suffix = hashlib.sha256(value.encode()).hexdigest()[:_HASH_SUFFIX_LEN]
        head = _ESXI_NAME_MAX - len(suffix) - 1
        cleaned = f"{cleaned[:head]}-{suffix}"
    return cleaned


def compose_resource_name(run_id: str, kind: str, name: str) -> str:
    """Deterministic backend name, sanitised to an ESXi inventory name.

    The VM variant is the name→MoRef recovery anchor (ADR-0008 §6): the driver
    stamps it into ``config.name`` and resolves it back on teardown.
    """
    return _esxi_name(f"tr-{kind}-{run_id[:8]}-{name}")


def compose_mac(plan_name: str, vm_name: str, nic_idx: int) -> str:
    """Deterministic MAC in VMware's manual range (00:50:56:00:00:00 onward).

    Pure: same ``(plan_name, vm_name, nic_idx)`` -> same MAC, so a stable MAC
    yields the same DHCP lease across runs (ADR-0006). The leading OUI is
    VMware's and the 4th octet is masked to ``0x3f`` because ESXi validates a
    ``manual`` NIC address against exactly this range and rejects anything else.
    The ``BUILD_NIC_NIC_IDX`` sentinel (``-1``) hashes directly like any index.
    """
    digest = hashlib.sha256(f"{plan_name}/{vm_name}/{nic_idx}".encode()).digest()
    octets = (*_VMWARE_OUI, digest[0] & _MANUAL_4TH_MASK, digest[1], digest[2])
    return ":".join(f"{b:02x}" for b in octets)


def volume_suffix(kind: str) -> str:
    return _SUFFIXES[kind]


def _backend_filename(vol_name: str) -> str:
    """The on-datastore filename for a logical volume name.

    Seeds/boot media (``.iso``) pass through. A disk's canonical ``.qcow2``
    cache name maps to the derived on-datastore ``.vmdk`` — the runnable VMFS
    disk the driver inflates; the qcow2 never lands on the datastore.
    """
    if vol_name.endswith(".iso"):
        return vol_name
    stem = vol_name.rsplit(".", 1)[0]
    return f"{stem}.vmdk"


def compose_volume_ref(datastore: str, pool_backend_name: str, vol_name: str) -> VolumeRef:
    """Pure ``VolumeRef`` keyed on ``(datastore, pool, vol_name)``.

    Datastore-path bracket form ``[<datastore>] <pool>/<filename>`` — what
    pyVmomi takes as a disk backing ``fileName`` / CDROM ISO path. The pool is a
    datastore *folder*; the filename is the ``.vmdk`` (disk) or ``.iso`` (seed).
    """
    filename = _backend_filename(vol_name)
    return VolumeRef(f"[{datastore}] {pool_backend_name}/{filename}")


_REF_RE = re.compile(r"^\[(?P<ds>[^\]]+)\]\s+(?P<path>.+)$")


def parse_volume_ref(ref: str) -> tuple[str, str]:
    """Recover ``(datastore, datastore_relative_path)`` from a bracket ``VolumeRef``.

    ``[datastore1] pool/web.vmdk`` → ``("datastore1", "pool/web.vmdk")``. The
    relative path is what the ``/folder`` HTTPS endpoint and the datastore file
    managers address.
    """
    m = _REF_RE.match(ref)
    if m is None:
        raise ValueError(f"not an ESXi datastore-path VolumeRef: {ref!r}")
    return m.group("ds"), m.group("path")


def ref_dir(ref: str) -> str:
    """The pool-folder (datastore-relative dir) a ref lives in (``pool``)."""
    _ds, path = parse_volume_ref(ref)
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _short(prefix: str, value: str) -> str:
    """A stable, length-safe backend object name: ``<prefix><8hex of value>``.

    Pure, so a ``from_uri`` teardown driver recomputes the same name from the
    composed backend name without any in-process map (ESXi vSwitch/portgroup
    names are length-limited; the orchestrator's composed names are too long).
    """
    digest = hashlib.sha1(value.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{prefix}{digest}"


def vswitch_name(switch_backend_name: str) -> str:
    """Stable standard-vSwitch name for a Switch's isolated L2 segment."""
    return _short("trs-", switch_backend_name)


def portgroup_name(network_backend_name: str) -> str:
    """Stable portgroup name a Network is realized as (on its Switch's vSwitch)."""
    return _short("trp-", network_backend_name)


def mgmt_portgroup_name(switch_backend_name: str) -> str:
    """Stable portgroup name carrying the host's mgmt ``.2`` VMkernel NIC."""
    return _short("trm-", switch_backend_name)


def uplink_vswitch_name(pnic: str) -> str:
    """Stable name of the **shared** uplink vSwitch that enslaves ``pnic``.

    Keyed on the physical NIC, not a Switch: a pNIC can belong to only one
    vSwitch, so every NAT Switch resolving to the same uplink (``egress`` ->
    ``vmnic1``) shares one uplink vSwitch, each with its own uplink portgroup.
    """
    return _short("tru-", pnic)


def uplink_portgroup_name(switch_backend_name: str) -> str:
    """Stable per-Switch portgroup on the shared uplink vSwitch (sidecar eth1)."""
    return _short("trx-", switch_backend_name)
