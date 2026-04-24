"""Abstract base class for *nested* virtual machines — VMs that are
also orchestrators.

A :class:`AbstractHypervisor` is an :class:`AbstractVM` that carries
three extra fields describing the environment it hosts inside itself:

- :attr:`orchestrator` — the class that drives the inner layer
- :attr:`vms` — :class:`AbstractVM` specs to run *inside* this VM
- :attr:`networks` — :class:`AbstractVirtualNetwork` specs for the
  inner layer

The outer orchestrator partitions its VM list by
``isinstance(vm, AbstractHypervisor)``; hypervisor VMs have their
inner orchestrators entered / exited around the test function.
"""

from __future__ import annotations

from abc import ABC
from typing import TYPE_CHECKING

from testrange.vms.base import AbstractVM

if TYPE_CHECKING:
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.orchestrator_base import AbstractOrchestrator


class AbstractHypervisor(AbstractVM, ABC):
    """A VM that is also an orchestrator for inner VMs + networks.

    Concrete subclasses live next to their backend's concrete
    :class:`AbstractVM` — e.g.
    :class:`testrange.backends.libvirt.hypervisor.Hypervisor` extends
    :class:`testrange.backends.libvirt.vm.VM` with the hypervisor
    payload (libvirt-daemon-system packages, post-install enable of
    ``libvirtd``, etc.) on top of the three fields declared here.

    The plain-VM spec fields (``iso``, ``users``, ``devices``, …) still
    describe this VM; the new fields describe the *environment it
    hosts*.
    """

    orchestrator: type[AbstractOrchestrator]
    """The orchestrator class that drives the inner layer.

    At outer-orchestrator :meth:`__enter__` time we call
    :meth:`~testrange.orchestrator_base.AbstractOrchestrator.root_on_vm`
    on this class to produce a concrete, entered inner orchestrator
    rooted on this VM.  Different drivers build their inner control
    plane in different ways (libvirt → ``qemu+ssh://``; Proxmox →
    ``https://host:8006`` + API token); the class itself encapsulates
    that difference.
    """

    vms: list[AbstractVM]
    """VM specs for the inner layer.

    Handed to :meth:`AbstractOrchestrator.root_on_vm` verbatim —
    the inner orchestrator treats them the same way the outer one
    treats its own ``vms`` argument.
    """

    networks: list[AbstractVirtualNetwork]
    """Virtual-network specs for the inner layer."""


__all__ = ["AbstractHypervisor"]
