"""Per-role build-artifact cache naming (ADR-0010 §4).

A built VM is captured as a *set* of cache entries — one per writable disk:
the OS disk plus each data disk in spec order. Each entry's name encodes the
VM's ``config_hash`` and the disk's role, so a VM is cached iff **every** role
is present (a partial set — OS present, a data disk missing — is a miss for the
whole VM). This replaces the single ``_post_install_<hash>`` name from the
install-phase design.
"""

from __future__ import annotations

_BUILT_PREFIX = "_built_"


def built_artifact_name(config_hash: str, role: str) -> str:
    """Cache name for one built disk: ``_built_<config_hash>__<role>``.

    ``role`` is ``"os"`` for the OS disk or ``"data<N>"`` (0-based, spec
    order) for a data disk. Pure: same inputs -> same name.
    """
    return f"{_BUILT_PREFIX}{config_hash}__{role}"


def data_disk_role(idx: int) -> str:
    """Role label for the ``idx``-th data disk (0-based, spec order)."""
    return f"data{idx}"


def built_artifact_roles(num_data_disks: int) -> tuple[str, ...]:
    """Ordered roles for a VM's writable-disk set: OS disk first, then data.

    ``("os",)`` for a VM with no data disks; ``("os", "data0", "data1")`` for
    two data disks.
    """
    return ("os", *(data_disk_role(i) for i in range(num_data_disks)))


__all__ = ["built_artifact_name", "built_artifact_roles", "data_disk_role"]
