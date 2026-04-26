"""Proxmox VE VM (SCAFFOLDING).

.. warning::

   Not yet implemented.  :meth:`build` and :meth:`start_run` raise
   :class:`NotImplementedError` with pointers at what still needs
   wiring up.

Design notes
------------

:class:`ProxmoxVM` consumes the same
:class:`~testrange.vms.builders.base.Builder` dataclasses
(:class:`InstallDomain`, :class:`RunDomain`) as
:class:`~testrange.backends.libvirt.VM` — builders are hypervisor-neutral.
The work here is translating those hints into Proxmox REST-API
parameters rather than libvirt domain XML.

Rough mapping (for the eventual implementation):

- ``InstallDomain.uefi=True`` → ``bios="ovmf"`` on ``POST /nodes/{node}/qemu``.
- ``InstallDomain.windows=True`` → ``ostype="win10"`` (or ``"win11"``);
  primary disk on ``ide0`` or ``sata0`` instead of ``virtio0``.
- ``InstallDomain.extra_cdroms`` → ``ide2``, ``ide3`` with
  ``media=cdrom``; upload of the ISO via
  ``POST /nodes/{node}/storage/{storage}/upload``.
- ``InstallDomain.seed_iso`` → an extra cdrom device.
- ``InstallDomain.boot_cdrom=True`` → ``boot="order=ide2;scsi0"``.

Completion detection mirrors libvirt: poll
``/nodes/{node}/qemu/{vmid}/status/current`` until ``status == "stopped"``,
matching the cloud-init ``power_state: poweroff`` or the Windows
``shutdown /s /t 0`` the builder appends.

After install, ``qm move_disk`` or storage-pool cloning captures the
post-install disk so subsequent runs boot from an overlay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.libvirt.vm import LibvirtVM as _LibvirtVM
from testrange.vms.base import AbstractVM

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.communication.base import AbstractCommunicator
    from testrange.orchestrator_base import AbstractOrchestrator


class ProxmoxVM(AbstractVM):
    """Proxmox-VE implementation of :class:`AbstractVM` (SCAFFOLDING).

    The constructor accepts the same arguments as
    :class:`~testrange.backends.libvirt.VM` and defers all lifecycle methods
    with :class:`NotImplementedError`.  Spec data lives on the instance
    so a future implementer only has to fill in the lifecycle methods.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Reuse the libvirt VM's constructor solely for its input
        # validation + device-list normalisation; the resulting
        # instance holds the same ``name``, ``iso``, ``users``,
        # ``builder``, ``communicator`` attributes the Proxmox
        # lifecycle methods will consult.  Swap this for a dedicated
        # dataclass when the spec and libvirt implementation drift.
        self._spec = _LibvirtVM(*args, **kwargs)  # type: ignore[arg-type]

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def builder(self):  # type: ignore[no-untyped-def]
        return self._spec.builder

    @property
    def users(self):  # type: ignore[no-untyped-def]
        """Forward to the underlying spec so
        :meth:`~testrange.vms.base.AbstractVM._make_communicator` can
        read credentials without the VM growing its own storage."""
        return self._spec.users

    # pyright flags these as "property overrides attribute" against
    # AbstractVM's plain-attribute declarations.  They're a deliberate
    # proxy pattern (ProxmoxVM wraps an internal _spec and forwards
    # reads); the ignore scope is exactly the override.
    @property
    def communicator(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Forward to the underlying spec (see :attr:`users`)."""
        return self._spec.communicator

    @property
    def _communicator(self) -> AbstractCommunicator | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._spec._communicator

    @_communicator.setter
    def _communicator(  # pyright: ignore[reportIncompatibleVariableOverride]
        self, value: AbstractCommunicator | None,
    ) -> None:
        self._spec._communicator = value

    def build(
        self,
        context: AbstractOrchestrator,
        cache: CacheManager,
        run: RunDir,
        install_network_name: str,
        install_network_mac: str,
    ) -> str:
        # TODO: if self.builder.needs_install_phase() is False, stage
        # the prebuilt qcow2 via self.builder.ready_image(...).  The
        # Proxmox twist: also need to import it into the Proxmox
        # storage pool via POST /nodes/{node}/storage/{storage}/upload
        # (or equivalent ``qm importdisk`` over SSH).
        # TODO: otherwise, consume self.builder.prepare_install_domain
        # and translate the InstallDomain fields into Proxmox REST
        # params (see module docstring for the mapping).
        # TODO: wait for status=stopped; snapshot the disk; return the
        # cached path (same CacheManager contract as libvirt).
        raise NotImplementedError(
            "ProxmoxVM.build is not yet implemented — see the "
            "testrange.backends.proxmox package docstring for the "
            "TODO list."
        )

    def start_run(
        self,
        context: AbstractOrchestrator,
        run: RunDir,
        installed_disk: str,
        network_entries: list[tuple[str, str]],
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> None:
        # TODO: consume self.builder.prepare_run_domain(...) and
        # translate the RunDomain fields into ``qm set`` / REST calls
        # on an overlay VMID.
        # TODO: attach a communicator via the same
        # _make_communicator() logic the libvirt VM uses, pointed at
        # the first static IP in mac_ip_pairs (WinRM / SSH) or via
        # the Proxmox guest-agent endpoint (future
        # ProxmoxGuestAgentCommunicator).
        raise NotImplementedError(
            "ProxmoxVM.start_run is not yet implemented."
        )

    def shutdown(self) -> None:
        # TODO: POST /nodes/{node}/qemu/{vmid}/status/stop; poll until
        # stopped; DELETE the VMID.
        raise NotImplementedError(
            "ProxmoxVM.shutdown is not yet implemented."
        )
