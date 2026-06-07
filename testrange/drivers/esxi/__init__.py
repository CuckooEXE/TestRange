"""Standalone ESXi driver (pyVmomi SOAP + datastore /folder HTTPS).

Public surface: :class:`ESXiHypervisor` (the Plan-time scheme marker),
:class:`ESXiDriver`, and :class:`ESXiProfile` (the ``driver = "esxi"`` connection
profile). Importing this package registers both the driver and the profile (the
``driver`` submodule calls ``register()`` and ``_profile`` calls
``register_profile()`` at import time).
"""

from __future__ import annotations

from testrange.drivers.esxi._profile import ESXiProfile
from testrange.drivers.esxi.driver import ESXiDriver, ESXiHypervisor

__all__ = ["ESXiDriver", "ESXiHypervisor", "ESXiProfile"]
