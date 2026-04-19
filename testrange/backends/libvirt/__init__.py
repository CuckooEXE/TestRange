"""libvirt / KVM / QEMU backend for TestRange.

This is backend zero — the default that the top-level package
symbols (:class:`testrange.Orchestrator`, :class:`testrange.VM`,
:class:`testrange.VirtualNetwork`) resolve to.  See
:doc:`/api/backends` for the abstract contracts every backend
satisfies and for the status of other backends.

Direct imports:

.. code-block:: python

    from testrange.backends.libvirt import (
        Orchestrator,
        VM,
        VirtualNetwork,
        GuestAgentCommunicator,
    )

are functionally identical to the top-level
:class:`testrange.Orchestrator` / :class:`testrange.VM` /
:class:`testrange.VirtualNetwork` — the top-level names are thin
re-exports of the names defined in this package.
"""

from testrange.backends.libvirt.guest_agent import GuestAgentCommunicator
from testrange.backends.libvirt.network import VirtualNetwork
from testrange.backends.libvirt.orchestrator import (
    LibvirtOrchestrator,
    Orchestrator,
)
from testrange.backends.libvirt.vm import VM

__all__ = [
    "GuestAgentCommunicator",
    "VirtualNetwork",
    "Orchestrator",
    "LibvirtOrchestrator",
    "VM",
]
