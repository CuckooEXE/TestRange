"""VM- and pool-level plan validation (the non-network half).

The network/addressing checks live in :mod:`testrange.networks.validate`;
this module owns what is about VM and pool *declarations*: VM-name
uniqueness and safety, the reserved ``__`` prefix and the ``-data<N>``
marker on VM names, and that every OSDrive references a declared pool.

:func:`testrange.networks.validate.validate_hypervisor_plan` is the entry
point a Hypervisor runs at construction; it delegates here for these checks.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from testrange.devices.pool.base import StoragePool
from testrange.networks.validate import validate_name
from testrange.vms.recipe import VMRecipe

# The orchestrator names a VM's i-th data disk ``<vm>-data<i>`` (see
# ``orchestrator/artifacts.py``), while its OS disk is just ``<vm>``. So a VM
# whose name ends in a ``-data<N>`` marker would produce an OS-disk volume ref
# identical to some *other* VM's data-disk ref — two distinct disks colliding on
# one backend locator (silent clobber on upload, mis-resolution on capture —
# PVE-30). The marker is backend-agnostic (it's the orchestrator's, not a
# driver's), so the guard lives here. ``[-_.]`` covers the separators a backend
# may fold to ``-`` when sanitizing (e.g. Proxmox lowercases and maps ``_``/``.``
# to ``-``), case-insensitively.
_DATA_DISK_MARKER = re.compile(r"[-_.]data\d+$", re.IGNORECASE)


def validate_vm_plan(vms: Iterable[VMRecipe], pools: Iterable[StoragePool]) -> None:
    """Validate VM and pool declarations. Raises ``ValueError`` on the first problem.

    Checks VM-name uniqueness and safety (:func:`validate_name`), the reserved
    ``__`` prefix, the reserved ``-data<N>`` marker, and that every OSDrive
    references a declared pool.
    """
    rs = tuple(vms)
    pool_names = {p.name for p in pools}
    vm_names = [r.name for r in rs]

    dup_vms = {n for n in vm_names if vm_names.count(n) > 1}
    if dup_vms:
        raise ValueError(f"hypervisor vms have duplicate names: {sorted(dup_vms)}")

    for r in rs:
        validate_name(r.name, "VMSpec.name")

    # The orchestrator synthesizes internal VMs under a `__` prefix
    # (__sidecar_<sw>); reserve it against user names.
    reserved = sorted({n for n in vm_names if n.startswith("__")})
    if reserved:
        raise ValueError(
            f"names starting with '__' are reserved for testrange internals; rename: {reserved}"
        )

    data_marked = sorted(n for n in vm_names if _DATA_DISK_MARKER.search(n))
    if data_marked:
        raise ValueError(
            "VM names ending in a '-data<N>' marker are reserved (the orchestrator "
            "names a VM's i-th data disk '<vm>-data<i>', so such a name would collide "
            f"with another VM's data-disk volume ref); rename: {data_marked}"
        )

    for r in rs:
        if r.spec.os_drive.pool not in pool_names:
            raise ValueError(
                f"VM {r.name!r} OSDrive references unknown pool {r.spec.os_drive.pool!r}; "
                f"declared pools: {sorted(pool_names)}"
            )


__all__ = ["validate_vm_plan"]
