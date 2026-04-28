"""Proxmox-flavoured concrete :class:`Hypervisor`.

Internal companion to the user-facing
:class:`testrange.vms.hypervisor.Hypervisor`.  The generic class is
``GenericVM + AbstractHypervisor``; this is its proxmox-shaped twin
(``ProxmoxVM + AbstractHypervisor``) with the lifecycle methods
:class:`~testrange.backends.proxmox.orchestrator.ProxmoxOrchestrator`'s
provisioning pipeline expects.

The translation happens inside
:func:`~testrange.backends.proxmox.orchestrator._promote_to_proxmox`
when the outer orchestrator instantiates: a generic
:class:`Hypervisor` carries its already-prepared spec (the inner
orchestrator's :meth:`prepare_outer_vm` ran at construction) into a
fresh instance of this class.

Most users don't import this directly — they use the top-level
:class:`testrange.Hypervisor`.  It's still exported as
``testrange.backends.proxmox.Hypervisor`` for callers that want to
pin to proxmox-shaped behaviour explicitly or for ``isinstance``
checks against a concrete proxmox VM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.proxmox.vm import ProxmoxVM
from testrange.vms.hypervisor_base import AbstractHypervisor

if TYPE_CHECKING:
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM
    from testrange.vms.builders import Builder


class Hypervisor(ProxmoxVM, AbstractHypervisor):
    """A proxmox-shaped VM that hosts an inner orchestrator.

    Instances of this class are produced by
    :func:`~testrange.backends.proxmox.orchestrator._promote_to_proxmox`
    when the outer orchestrator is :class:`ProxmoxOrchestrator`; user
    code should normally construct :class:`testrange.Hypervisor` (the
    backend-neutral entry point) instead.

    The constructor accepts the regular VM kwargs plus the three
    :class:`AbstractHypervisor` data fields.  Unlike a libvirt-on-X
    hypervisor, no payload is auto-injected here — the PVE installer
    is the whole install phase, so most ``prepare_outer_vm`` overrides
    leave the spec untouched and the constructor just stores fields.
    """

    orchestrator: type[AbstractOrchestrator]
    vms: list[AbstractVM]  # pyright: ignore[reportIncompatibleVariableOverride]
    networks: list[AbstractVirtualNetwork]

    def __init__(
        self,
        name: str,
        iso: str,
        users: list[Credential],
        orchestrator: type[AbstractOrchestrator],
        vms: list[AbstractVM] | None = None,
        networks: list[AbstractVirtualNetwork] | None = None,
        pkgs: list[AbstractPackage] | None = None,
        post_install_cmds: list[str] | None = None,
        devices: list[AbstractDevice] | None = None,
        builder: Builder | None = None,
        communicator: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            iso=iso,
            users=users,
            pkgs=pkgs,
            post_install_cmds=post_install_cmds,
            devices=devices,
            builder=builder,
            communicator=communicator,
        )
        self.orchestrator = orchestrator
        self.vms = list(vms or [])
        self.networks = list(networks or [])


__all__ = ["Hypervisor"]
