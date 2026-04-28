"""Backend-neutral :class:`Hypervisor` — top-level user API.

A :class:`Hypervisor` is a :class:`~testrange.vms.generic.GenericVM`
(so the outer orchestrator's normal VM-promotion pipeline knows how
to convert it to its native VM class) plus the three data fields
required by :class:`~testrange.vms.hypervisor_base.AbstractHypervisor`
(``orchestrator`` / ``vms`` / ``networks``).

The interesting behaviour: the constructor calls
:meth:`AbstractOrchestrator.prepare_outer_vm` on the inner
*orchestrator* class, letting *it* declare what software the outer
VM needs in order to host an inner instance of it.  The libvirt
orchestrator stamps ``libvirt-daemon-system`` + a ``systemctl enable
libvirtd`` post-install hook; the Proxmox orchestrator stamps
nothing because the PVE installer is the whole install phase.

This separation gets the cross-product of outer/inner backends
right without spawning a concrete Hypervisor class for every
combination:

- Outer libvirt + inner libvirt → libvirt-daemon-system pkgs land
  in the Hypervisor spec at construction; the libvirt orchestrator
  promotes the spec into its libvirt-flavoured concrete Hypervisor;
  cloud-init installs the pkgs; libvirtd runs.
- Outer libvirt + inner Proxmox → no pkgs land; the libvirt
  orchestrator promotes the (empty-pkgs) spec; ProxmoxAnswerBuilder
  runs the PVE installer; cache hash is honest because no dead
  ``libvirt-daemon-system`` hangs around the cache key.
- Outer Proxmox + inner libvirt → libvirt-daemon-system pkgs land
  in the spec; Proxmox orchestrator promotes into its proxmox-shaped
  concrete Hypervisor; the post-install pipeline applies the pkgs.
- Outer Proxmox + inner Proxmox → no pkgs land; Proxmox installer
  is the whole phase; clean.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.exceptions import OrchestratorError
from testrange.vms.generic import GenericVM
from testrange.vms.hypervisor_base import AbstractHypervisor

if TYPE_CHECKING:
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM
    from testrange.vms.builders.base import Builder


def _check_inner_name_duplicates(
    vms: Sequence[AbstractVM],
    networks: Sequence[AbstractVirtualNetwork],
) -> None:
    """Plain duplicate-name detection for the inner layer.

    Backend-agnostic: only catches exact-match collisions on the
    ``name`` attribute (the kind that would silently overwrite
    entries in the inner orchestrator's ``vms`` / ``networks``
    dicts).  Backend-specific stricter checks (libvirt's 10-char
    domain-name truncation, 6-char network-name truncation) live in
    the per-backend concrete Hypervisor and run again at promote
    time, so this is a first-pass guard not the only line of
    defence.
    """
    seen_vm: set[str] = set()
    for vm in vms:
        if vm.name in seen_vm:
            raise OrchestratorError(
                f"duplicate VM name {vm.name!r} in hypervisor inner "
                f"VM list"
            )
        seen_vm.add(vm.name)
    seen_net: set[str] = set()
    for net in networks:
        if net.name in seen_net:
            raise OrchestratorError(
                f"duplicate network name {net.name!r} in hypervisor "
                f"inner network list"
            )
        seen_net.add(net.name)


class Hypervisor(GenericVM, AbstractHypervisor):
    """A VM that hosts an inner orchestrator.

    .. code-block:: python

        from testrange import Hypervisor, VM, VirtualNetwork
        from testrange.backends.proxmox import ProxmoxOrchestrator

        hv = Hypervisor(
            name="proxmox",
            iso="https://.../proxmox-ve.iso",
            users=[Credential("root", "...")],
            devices=[Memory(4), HardDrive(64), vNIC("OuterNet", ip="10.0.0.10")],
            orchestrator=ProxmoxOrchestrator,
            vms=[
                VM(name="inner-web", iso="...", users=[...], devices=[...]),
            ],
            networks=[
                VirtualNetwork("InnerNet", "10.42.0.0/24", internet=True),
            ],
        )

    :param name: VM name in the outer orchestrator's namespace.
    :param iso: Boot image for the outer VM.  Auto-selects a builder
        based on filename (PVE installer ISO →
        :class:`ProxmoxAnswerBuilder`; everything else cloud-init).
    :param users: Credentials provisioned on the outer VM.
    :param orchestrator: The orchestrator class that will drive the
        inner layer once this VM is up.  Its
        :meth:`~AbstractOrchestrator.prepare_outer_vm` is invoked
        immediately to stamp this hypervisor's outer-VM requirements.
    :param vms: VM specs to run inside this hypervisor.  Handed to
        :meth:`AbstractOrchestrator.root_on_vm` after the outer VM
        is reachable.
    :param networks: Virtual-network specs for the inner layer.
    :param pkgs: Extra packages on top of whatever the inner
        orchestrator class injects.  Forwarded to the underlying
        :class:`GenericVM`.
    :param post_install_cmds: Extra post-install commands on top of
        whatever the inner orchestrator class injects.
    :param devices: Virtual hardware for the outer VM (vCPU, Memory,
        HardDrive, vNIC).
    :param builder: Explicit
        :class:`~testrange.vms.builders.base.Builder` strategy.  When
        ``None`` the registry's auto-selector picks one from ``iso``.
    :param communicator: ``"ssh"`` / ``"guest-agent"`` / ``"winrm"``,
        or ``None`` to let the builder default it.
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
        _check_inner_name_duplicates(self.vms, self.networks)
        # Inner orchestrator gets the chance to declare what software
        # this outer VM needs to host it (apt packages, post-install
        # hooks, group memberships, …).  Default is a no-op (suits
        # any inner whose installer ISO is self-contained, like PVE).
        orchestrator.prepare_outer_vm(self)


__all__ = ["Hypervisor"]
