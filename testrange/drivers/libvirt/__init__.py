"""libvirt backend (BACKEND-1).

Exposes ``LibvirtHypervisor`` (the Plan-time entry) and ``LibvirtDriver``.
Importing this package registers the driver with the registry (a ``register()``
call at ``driver`` module scope). libvirt-python and pyroute2 import lazily
(only on ``connect()`` / L2), so the package registers without them installed.
"""

from __future__ import annotations

from testrange.drivers.libvirt.driver import LibvirtDriver, LibvirtHypervisor

__all__ = ["LibvirtDriver", "LibvirtHypervisor"]
