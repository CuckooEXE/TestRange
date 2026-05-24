"""Proxmox VE driver (proxmoxer REST + paramiko SFTP).

Public surface: :class:`ProxmoxHypervisor` (the Plan-time entry) and
:class:`ProxmoxDriver`. Importing this package registers the driver with the
registry (the ``driver`` submodule calls ``register()`` at import time).
"""

from __future__ import annotations

from testrange.drivers.proxmox.driver import ProxmoxDriver, ProxmoxHypervisor

__all__ = ["ProxmoxDriver", "ProxmoxHypervisor"]
