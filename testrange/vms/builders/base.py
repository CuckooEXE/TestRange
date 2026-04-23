"""Builder abstraction for the VM provisioning pipeline.

Each concrete :class:`Builder` encodes how a particular flavour of VM
gets from ``iso=`` to a runnable disk image.  The two "install-phase"
builders produce a cached post-install disk by booting a one-off
domain (cloud-init for Linux cloud images, Windows Setup + autounattend
for Windows install ISOs); the :class:`NoOpBuilder` skips the install
entirely and just stages a user-supplied qcow2.

:class:`~testrange.backends.libvirt.VM` holds a ``builder`` attribute and
delegates everything install-pipeline related to it:

1. **Disk prep** — :meth:`Builder.prepare_install_domain` returns the
   :class:`InstallDomain` spec (primary disk, optional seed ISO, extra
   CD-ROMs, and domain-XML hints).
2. **Post-install caching** — :meth:`Builder.install_manifest` populates
   the sibling JSON manifest stored next to
   ``<cache_root>/vms/<config_hash>.qcow2``.
3. **Run phase** — :meth:`Builder.prepare_run_domain` returns the
   :class:`RunDomain` spec.  No disk work here — the VM overlay is
   created by :class:`~testrange._run.RunDir` before the builder is
   consulted.

Builders that can skip the install phase return ``False`` from
:meth:`Builder.needs_install_phase` and implement
:meth:`Builder.ready_image` instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.backends.libvirt.vm import VM
    from testrange.cache import CacheManager


@dataclass(frozen=True)
class InstallDomain:
    """Instructions :class:`~testrange.backends.libvirt.VM` needs to build
    the install-phase libvirt domain XML.

    All disk-like fields are **backend-local refs** — strings that the
    hypervisor's host can open directly.  For the default
    :class:`~testrange.storage.LocalStorageBackend` these are absolute
    outer-host paths (behaviourally identical to the pre-backend
    code); for remote backends they are paths on the remote host
    where the uploaded qcow2s live.

    :param work_disk: Backend-local ref to the primary disk the
        install domain will boot and write to.  Cloud-init uses an
        overlay on the resolved base image; autounattend uses a blank
        qcow2.
    :param seed_iso: Optional backend-local ref to the seed CD-ROM
        (cloud-init or autounattend seed) attached as the first
        CD-ROM device.
    :param extra_cdroms: Additional CD-ROM refs, in SATA-target order
        after the seed.  Used by Windows to surface the install ISO
        and the ``virtio-win.iso`` driver disc.
    :param uefi: If ``True``, emit ``<loader>`` + ``<nvram>`` OVMF
        references.  Windows requires this for GPT installs.
    :param windows: If ``True``, use device models with built-in
        Windows Setup drivers: SATA primary disk, e1000e NIC.
    :param boot_cdrom: If ``True``, boot from CD-ROM before disk — the
        Windows installer needs this; Linux cloud-init boots straight
        off the qcow2.
    """

    work_disk: str
    seed_iso: str | None = None
    extra_cdroms: tuple[str, ...] = ()
    uefi: bool = False
    windows: bool = False
    boot_cdrom: bool = False


@dataclass(frozen=True)
class RunDomain:
    """Instructions :class:`~testrange.backends.libvirt.VM` needs to build
    the run-phase libvirt domain XML.

    :param seed_iso: Optional backend-local ref to the run-phase seed
        (phase-2 cloud-init).  ``None`` for Windows + NoOp — neither
        needs to inject anything on re-boot.
    :param uefi: Firmware family; same semantics as
        :attr:`InstallDomain.uefi`.  Must match whatever the install
        phase used or the cached disk won't boot.
    :param windows: Same semantics as :attr:`InstallDomain.windows`.
    """

    seed_iso: str | None = None
    uefi: bool = False
    windows: bool = False


class Builder(ABC):
    """Strategy for provisioning a :class:`~testrange.backends.libvirt.VM`.

    Concrete implementations live in :mod:`testrange.vms.builders`.
    Subclass this to support a new install mechanism (preseed,
    Kickstart, Ignition, sysprep'd Windows, etc.).

    Builders are stateless: everything they need to know about a
    specific VM is passed in through ``vm`` or computed from the
    :class:`~testrange.cache.CacheManager` / :class:`~testrange._run.RunDir`
    argument.  One builder instance can safely serve many VMs.
    """

    @abstractmethod
    def default_communicator(self) -> str:
        """Return the default communicator kind for VMs using this builder.

        Chosen by :class:`~testrange.backends.libvirt.VM` when the caller
        does not pass ``communicator=``.  Typical values:
        ``"guest-agent"``, ``"ssh"``, ``"winrm"``.
        """

    def needs_install_phase(self) -> bool:
        """Whether to boot a one-off install-phase domain for this VM.

        Default is ``True``; :class:`NoOpBuilder`-style implementations
        override to ``False``.  When ``False``,
        :meth:`~testrange.backends.libvirt.VM.build` calls
        :meth:`ready_image` instead of going through the install flow.
        """
        return True

    def needs_boot_keypress(self) -> bool:
        """Whether the install domain needs spacebars spammed at boot.

        Some install media (notably Windows install ISOs under UEFI)
        show a time-limited "Press any key to boot from CD or DVD..."
        prompt.  A VM has no physical keyboard to press it, so the
        prompt times out, OVMF falls through to the empty hard disk,
        and then drops to the EFI shell.  Builders that need boot
        keypresses override this to ``True`` and
        :meth:`~testrange.backends.libvirt.VM._run_install_phase`
        spawns a short-lived thread that sends spacebars via
        :meth:`virDomain.sendKey` during the early boot window.

        Default is ``False``.
        """
        return False

    @abstractmethod
    def cache_key(self, vm: VM) -> str:
        """Return the content-or-config hash under which the
        post-install disk is cached.

        Install-phase builders typically wrap
        :func:`~testrange.cache.vm_config_hash` over the VM's spec.
        Builders that do not use the install cache (NoOp) may raise
        :class:`NotImplementedError`.
        """

    @abstractmethod
    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> InstallDomain:
        """Prepare every on-disk artifact the install domain needs.

        Implementations resolve / stage the base image, create the
        working disk, build the seed ISO, fetch auxiliary ISOs (e.g.
        ``virtio-win``), and return an :class:`InstallDomain` whose
        ``work_disk`` will be snapshotted into the cache after the
        domain powers off.

        Only called when :meth:`needs_install_phase` returns ``True``.
        """

    @abstractmethod
    def install_manifest(
        self,
        vm: VM,
        config_hash: str,
    ) -> dict[str, Any]:
        """Return the JSON-serialisable manifest written next to the
        cached post-install disk.  Lets humans inspect what's in a
        cached image without booting it.
        """

    @abstractmethod
    def prepare_run_domain(
        self,
        vm: VM,
        run: RunDir,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> RunDomain:
        """Return the run-phase domain hints.

        Called on every run.  Overlays on the cached installed disk are
        created by :class:`~testrange._run.RunDir` before this is
        invoked, so the builder only needs to decide whether to inject
        a run-phase seed and which firmware/model to declare.
        """

    def ready_image(
        self,
        vm: VM,
        cache: CacheManager,
        run: RunDir,
    ) -> str:
        """For :meth:`needs_install_phase`-``False`` builders, return
        the backend-local ref for a disk that is already ready to boot.

        The *run* parameter exposes the storage backend (via
        ``run.storage``) so implementations can stage bring-your-own
        images onto remote backends.

        The default implementation raises — install-phase builders
        should never hit this path.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.ready_image() is only meaningful for "
            "builders whose needs_install_phase() returns False."
        )


__all__ = ["Builder", "InstallDomain", "RunDomain"]
