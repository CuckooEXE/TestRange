"""HypervisorDriver ABC — Phase 2 introduces the runtime methods.

This file is intentionally tiny in Phase 0; concretes import nothing from
here yet. The full ABC arrives in Phase 2.
"""

from __future__ import annotations

from abc import ABC


class HypervisorDriver(ABC):  # noqa: B024  (Phase 2 fills in abstract methods)
    """Abstract base for hypervisor backends (libvirt, proxmox, esxi, ...)."""
