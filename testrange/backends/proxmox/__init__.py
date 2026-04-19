"""Proxmox VE backend for TestRange (SCAFFOLDING — not yet implemented).

Importing this package succeeds without the Proxmox Python client
(``proxmoxer``) being installed.  All heavy lifting is deferred to
:meth:`ProxmoxOrchestrator.__enter__`, which raises
:class:`NotImplementedError` with a clear message explaining what still
needs to be wired up.

Once implementation lands, the package-level API mirrors the libvirt
backend:

.. code-block:: python

    from testrange.backends.proxmox import (
        ProxmoxOrchestrator,
        ProxmoxVM,
        ProxmoxVirtualNetwork,
    )

    with ProxmoxOrchestrator(host="pve.example.com", ...) as orch:
        orch.vms["web"].exec([...])

See :mod:`testrange.backends.proxmox.orchestrator` for the full TODO
list.
"""

from testrange.backends.proxmox.guest_agent import (
    ProxmoxGuestAgentCommunicator,
)
from testrange.backends.proxmox.network import ProxmoxVirtualNetwork
from testrange.backends.proxmox.orchestrator import ProxmoxOrchestrator
from testrange.backends.proxmox.vm import ProxmoxVM

__all__ = [
    "ProxmoxOrchestrator",
    "ProxmoxVM",
    "ProxmoxVirtualNetwork",
    "ProxmoxGuestAgentCommunicator",
]
