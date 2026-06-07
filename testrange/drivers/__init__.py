"""Hypervisor drivers.

Public surface: the ``HypervisorDriver`` ABC plus one concrete driver per
supported hypervisor. Each driver module also exposes a Plan-time
hypervisor data type (e.g., ``LibvirtHypervisor``) used as the top-level
entry of a ``Plan``.

Driver modules are imported here so they register themselves with the
driver registry. To add a new driver, add it as a submodule and import it
below. (The in-memory ``mock`` backend is **test-only**: it lives under
``tests/`` and is registered by ``tests/conftest.py``, not here. libvirt is
the reference implementation.)
"""

from __future__ import annotations

# Side-effect imports: each driver module calls register() at module scope.
# Every backend package imports cleanly without its SDK installed — the SDK
# imports (proxmoxer / libvirt-python / pyvmomi) are lazy (only on connect()),
# so registration costs nothing at import.
from testrange.drivers import esxi as _esxi  # noqa: F401
from testrange.drivers import libvirt as _libvirt  # noqa: F401
from testrange.drivers import proxmox as _proxmox  # noqa: F401
from testrange.drivers._registry import (
    driver_for_name,
    is_pinned,
    register,
    scheme_for_hypervisor,
)
from testrange.drivers.base import HypervisorDriver

__all__ = [
    "HypervisorDriver",
    "driver_for_name",
    "is_pinned",
    "register",
    "scheme_for_hypervisor",
]
