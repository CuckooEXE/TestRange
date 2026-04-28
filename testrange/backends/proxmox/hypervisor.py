"""Proxmox-backed :class:`ProxmoxHypervisor` — a VM that hosts an inner
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator`.

The PVE installer is the whole install phase: ``pveproxy``,
``pvedaemon``, ``qm``, and the SDN stack are pre-installed and started
by the PVE installer itself.  An inner
:class:`ProxmoxOrchestrator` reaches the running PVE control plane
over its REST API on port 8006 — so this class adds **no** package or
post-install-command injection on top of the base :class:`ProxmoxVM`,
unlike :class:`testrange.backends.libvirt.Hypervisor` which has to
install ``libvirt-daemon-system`` and start ``libvirtd`` on a plain
Debian guest.

The three extra fields required by :class:`AbstractHypervisor`
(``orchestrator``, ``vms``, ``networks``) are accepted as keyword
arguments alongside the usual :class:`ProxmoxVM` ones.
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


class ProxmoxHypervisor(ProxmoxVM, AbstractHypervisor):
    """A Proxmox VE VM that hosts an inner :class:`ProxmoxOrchestrator`.

    .. code-block:: python

        from testrange import Hypervisor, VM, VirtualNetwork
        from testrange import Credential, Memory, HardDrive, vNIC
        from testrange.backends.proxmox import ProxmoxOrchestrator

        # Top-level ``Hypervisor`` factory dispatches to this class
        # because ``orchestrator=ProxmoxOrchestrator``.
        hv = Hypervisor(
            name="proxmox",
            iso="https://enterprise.proxmox.com/.../proxmox-ve_9.0-1.iso",
            users=[Credential("root", "Password123!")],
            devices=[
                Memory(4),
                HardDrive(64),
                vNIC("OuterNet", ip="10.0.0.10"),
            ],
            orchestrator=ProxmoxOrchestrator,
            vms=[
                VM(name="inner", iso="...", users=[...], devices=[...]),
            ],
            networks=[
                VirtualNetwork("InnerNet", "10.42.0.0/24", internet=True),
            ],
        )

    The :attr:`vms` and :attr:`networks` fields describe the inner
    layer; everything else describes *this* VM.  No package injection
    happens — the PVE installer brings up the entire control plane,
    so the inner orchestrator's REST endpoint is reachable as soon as
    the VM finishes installing.

    :param orchestrator: The orchestrator class that drives the inner
        layer.  Almost always
        :class:`~testrange.backends.proxmox.ProxmoxOrchestrator`.
    :param vms: VM specs to run inside this hypervisor.
    :param networks: Virtual-network specs for the inner layer.

    All other parameters are forwarded to :class:`ProxmoxVM`; see its
    docstring for the full list.
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


__all__ = ["ProxmoxHypervisor"]
