"""Deterministic naming for the libvirt backend.

Pure functions only — same inputs, same outputs (the cleanup walker rebuilds
refs without live state). libvirt object names (domains, networks, pools,
volumes) tolerate ``[A-Za-z0-9_.+-]``; arbitrary plan names are sanitised down
to that, with a short hash appended when sanitisation/truncation would otherwise
collide distinct inputs.
"""

from __future__ import annotations

import hashlib
import re

from testrange.drivers.base import VolumeRef

# Locally-administered, unicast OUI (bit 0x02 of the first octet set), matching
# the other backends so a stable MAC yields the same DHCP lease across runs
# (ADR-0006). libvirt accepts any unicast MAC in a domain's <interface>.
_OUI_FIRST = 0x02

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

# libvirt names are permissive; collapse anything outside this set to a hyphen.
_NOT_LV = re.compile(r"[^A-Za-z0-9_.-]+")
_LV_NAME_MAX = 60
_HASH_SUFFIX_LEN = 6  # chars of sha256 appended on sanitisation/truncation


def _lv_name(value: str) -> str:
    cleaned = _NOT_LV.sub("-", value).strip("-") or "x"
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if cleaned != value or len(cleaned) > _LV_NAME_MAX:
        suffix = hashlib.sha256(value.encode()).hexdigest()[:_HASH_SUFFIX_LEN]
        # Reserve the suffix + its joining '-' so the result fits the cap; derive
        # the head width from len(suffix) so the two never desync if the hash
        # width changes.
        head = _LV_NAME_MAX - len(suffix) - 1
        cleaned = f"{cleaned[:head]}-{suffix}"
    return cleaned


def compose_resource_name(run_id: str, kind: str, name: str) -> str:
    """Deterministic backend name, sanitised to a libvirt-safe object name.

    The VM variant is the name→domain recovery anchor: the driver names the
    domain this and looks it up by name on teardown.
    """
    return _lv_name(f"tr-{kind}-{run_id[:8]}-{name}")


def compose_mac(plan_name: str, vm_name: str, nic_idx: int) -> str:
    digest = hashlib.sha256(f"{plan_name}/{vm_name}/{nic_idx}".encode()).digest()
    octets = [_OUI_FIRST, *digest[:5]]
    return ":".join(f"{b:02x}" for b in octets)


def compose_volume_ref(pool_backend_name: str, vol_name: str) -> VolumeRef:
    """Pure ``VolumeRef`` keyed on ``(pool, vol_name)``.

    A libvirt volume is addressed as ``<pool>/<volume>``; the storage module
    resolves a ref back to a live ``virStorageVol`` via the pool's
    ``storageVolLookupByName``. Pure: same inputs → same ref.
    """
    return VolumeRef(f"{pool_backend_name}/{vol_name}")


def volume_suffix(kind: str) -> str:
    return _SUFFIXES[kind]
