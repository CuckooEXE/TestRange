"""Libvirt-flavoured concrete :class:`Hypervisor`.

Internal companion to the user-facing
:class:`testrange.vms.hypervisor.Hypervisor`.  The generic class is
``GenericVM + AbstractHypervisor``; this is its libvirt-shaped twin
(``LibvirtVM + AbstractHypervisor``) with the lifecycle methods
:class:`~testrange.backends.libvirt.orchestrator.LibvirtOrchestrator`'s
provisioning pipeline expects (``_memory_kib``, ``build``,
``start_run``, ``shutdown``, etc.).

The translation happens inside
:func:`~testrange.backends.libvirt.orchestrator._promote_to_libvirt`
when the outer orchestrator instantiates: a generic
:class:`Hypervisor` carries its already-prepared spec (the inner
orchestrator's :meth:`prepare_outer_vm` ran at construction time, so
``pkgs`` / ``post_install_cmds`` are final) into a fresh instance of
this class.

Most users don't import this directly — they use the top-level
:class:`testrange.Hypervisor`.  It's still exported as
``testrange.backends.libvirt.Hypervisor`` for callers that want to
pin to libvirt-shaped behaviour explicitly or for ``isinstance``
checks against a concrete libvirt VM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.libvirt.orchestrator import check_name_collisions
from testrange.backends.libvirt.vm import LibvirtVM
from testrange.vms.hypervisor_base import AbstractHypervisor

if TYPE_CHECKING:
    from testrange.backends.libvirt.vm import LibvirtAcceptedDevice
    from testrange.credentials import Credential
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM
    from testrange.vms.builders import Builder


class Hypervisor(LibvirtVM, AbstractHypervisor):
    """A libvirt-shaped VM that hosts an inner orchestrator.

    Instances of this class are produced by
    :func:`~testrange.backends.libvirt.orchestrator._promote_to_libvirt`
    when the outer orchestrator is libvirt; user code should normally
    construct :class:`testrange.Hypervisor` (the backend-neutral
    factory) instead.

    The constructor takes both the regular VM kwargs and the three
    :class:`AbstractHypervisor` data fields.  Unlike the previous
    incarnation of this class, no payload (``libvirt-daemon-system``
    apt packages, ``systemctl enable libvirtd`` post-install hook,
    libvirt/kvm group additions) is injected here — that lives on
    :meth:`LibvirtOrchestrator.prepare_outer_vm` and runs at the
    generic Hypervisor's construction site, so by the time we get
    here the ``pkgs`` / ``post_install_cmds`` lists already reflect
    whatever the inner orchestrator class declared.
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
        devices: list["LibvirtAcceptedDevice"] | None = None,
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
        check_name_collisions(self.vms, self.networks)


__all__ = ["Hypervisor"]
