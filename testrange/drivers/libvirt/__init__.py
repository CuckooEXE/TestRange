"""libvirt backend (BACKEND-1).

Exposes ``LibvirtHypervisor`` (the Plan-time entry), ``LibvirtDriver``, and
``LibvirtProfile`` (the ``driver = "libvirt"`` connection profile). Importing
this package registers both the driver and the profile (a ``register()`` call
at ``driver`` module scope and a ``register_profile()`` call in ``_profile``).
libvirt-python is the sole libvirt dependency and imports lazily (only on
``connect()``), so the package registers without it installed.
"""

from __future__ import annotations

from testrange.drivers.libvirt._profile import LibvirtProfile
from testrange.drivers.libvirt.driver import LibvirtDriver, LibvirtHypervisor

__all__ = ["LibvirtDriver", "LibvirtHypervisor", "LibvirtProfile"]
