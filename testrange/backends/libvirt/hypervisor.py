"""libvirt-backed :class:`Hypervisor` — a VM that hosts an inner
libvirt orchestrator.

A :class:`Hypervisor` is a regular libvirt
:class:`~testrange.backends.libvirt.vm.VM` pre-loaded with the
packages and post-install steps needed to run ``libvirtd`` inside
it:

- ``libvirt-daemon-system``, ``qemu-kvm``, ``qemu-utils`` via apt
- ``systemctl enable --now libvirtd`` so the daemon is reachable on
  first boot
- members of the ``libvirt`` group for every declared user so that
  ``qemu+ssh://`` connections work without sudo
- the ``default`` libvirt NAT network started so inner VMs can reach
  the outside world

The three extra fields required by :class:`AbstractHypervisor`
(``orchestrator``, ``vms``, ``networks``) are accepted as keyword
arguments alongside the usual :class:`VM` ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.libvirt.vm import VM
from testrange.packages import Apt
from testrange.vms.hypervisor_base import AbstractHypervisor

if TYPE_CHECKING:
    from testrange.credentials import Credential
    from testrange.devices import AbstractDevice
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.packages import AbstractPackage
    from testrange.vms.base import AbstractVM
    from testrange.vms.builders import Builder


_HYPERVISOR_PKGS: tuple[str, ...] = (
    "libvirt-daemon-system",
    "qemu-kvm",
    "qemu-utils",
    "libvirt-clients",
)
"""APT packages pre-installed on every hypervisor VM.

Kept minimal on purpose: this is the set ``virsh -c qemu:///system``
needs to respond to a ``qemu+ssh://`` connection and run domains
built by the inner orchestrator.  Heavier tooling (``virtinst``,
``libguestfs``) is not needed — TestRange drives libvirt directly
through its Python bindings.
"""


def _default_post_install_cmds(users: list[Credential]) -> list[str]:
    """Return the post-install shell commands that get libvirtd
    reachable for the inner orchestrator.

    - Start + enable ``libvirtd``.
    - Start the ``default`` libvirt NAT network so inner VMs have
      upstream connectivity out of the box.
    - Add each user in ``users`` to the ``libvirt`` and ``kvm``
      groups so the inner ``qemu+ssh://user@.../system`` URI resolves
      without sudo.
    """
    cmds: list[str] = [
        "systemctl enable --now libvirtd",
        # ``net-autostart default`` is idempotent; ``net-start`` fails
        # harmlessly if the network is already active, which is fine
        # for a one-shot post-install hook.
        "virsh net-autostart default || true",
        "virsh net-start default || true",
    ]
    for cred in users:
        cmds.append(
            f"usermod -aG libvirt,kvm {cred.username}"
        )
    return cmds


class Hypervisor(VM, AbstractHypervisor):
    """A libvirt VM that hosts an inner libvirt orchestrator.

    .. code-block:: python

        from testrange import Hypervisor, LibvirtOrchestrator, VM
        from testrange import Credential, VirtualNetworkRef, HardDrive, Memory

        hv = Hypervisor(
            name="hv",
            iso="https://cloud.debian.org/.../debian-12-generic-amd64.qcow2",
            users=[Credential("root", "Password123!")],
            devices=[
                Memory(8),
                HardDrive(80),
                VirtualNetworkRef("OuterNet", ip="10.0.0.10"),
            ],
            orchestrator=LibvirtOrchestrator,
            vms=[
                VM(
                    name="inner-web",
                    iso="https://cloud.debian.org/.../debian-12-generic-amd64.qcow2",
                    users=[Credential("root", "Password123!")],
                    devices=[VirtualNetworkRef("InnerNet", ip="10.42.0.5")],
                ),
            ],
            networks=[
                VirtualNetwork("InnerNet", "10.42.0.0/24", internet=True),
            ],
        )

    The :attr:`vms` and :attr:`networks` fields describe the inner
    layer; everything else describes *this* VM.  The packages and
    post-install commands needed to run ``libvirtd`` are injected
    automatically — pass ``pkgs=`` / ``post_install_cmds=`` to add
    more on top.

    :param orchestrator: The orchestrator class that drives the inner
        layer.  Most callers will pass
        :class:`~testrange.backends.libvirt.Orchestrator`.
    :param vms: VM specs to run inside this hypervisor.
    :param networks: Virtual-network specs for the inner layer.

    All other parameters are forwarded to
    :class:`~testrange.backends.libvirt.vm.VM`; see its docstring for
    the full list.
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
        # Pre-pend libvirtd-enablement steps so caller-supplied
        # post_install_cmds still run, but only once libvirtd is up.
        merged_pkgs: list[AbstractPackage] = [
            Apt(p) for p in _HYPERVISOR_PKGS
        ] + list(pkgs or [])
        merged_post: list[str] = (
            _default_post_install_cmds(users) + list(post_install_cmds or [])
        )
        super().__init__(
            name=name,
            iso=iso,
            users=users,
            pkgs=merged_pkgs,
            post_install_cmds=merged_post,
            devices=devices,
            builder=builder,
            communicator=communicator,
        )
        self.orchestrator = orchestrator
        self.vms = list(vms or [])
        self.networks = list(networks or [])


__all__ = ["Hypervisor"]
