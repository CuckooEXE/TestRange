"""Proxmox VE driver (proxmoxer REST + paramiko SFTP).

Public surface: :class:`ProxmoxHypervisor` (the Plan-time entry),
:class:`ProxmoxDriver`, :class:`ProxmoxProfile` (the ``driver = "proxmox"``
connection profile), and :class:`ProxmoxHardDrive` (a PVE-specific data disk
with a selectable controller bus). Importing this package registers both the
driver and the profile (the ``driver`` submodule calls ``register()`` and
``_profile`` calls ``register_profile()`` at import time).
"""

from __future__ import annotations

from testrange.drivers.proxmox._profile import ProxmoxProfile
from testrange.drivers.proxmox.devices import ProxmoxHardDrive
from testrange.drivers.proxmox.driver import ProxmoxDriver, ProxmoxHypervisor

__all__ = ["ProxmoxDriver", "ProxmoxHardDrive", "ProxmoxHypervisor", "ProxmoxProfile"]
