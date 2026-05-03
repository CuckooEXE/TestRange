"""Builder abstraction for the VM provisioning pipeline.

Each concrete :class:`Builder` encodes how a particular flavour of VM
gets from ``iso=`` to a runnable disk image.  The two "install-phase"
builders produce a cached post-install disk by booting a one-off
domain (cloud-init for Linux cloud images, autounattend for Windows
install ISOs); the :class:`NoOpBuilder` skips the install entirely
and just stages a user-supplied prebuilt image.

Every VM spec holds a ``builder`` attribute and delegates the
install-pipeline work to it — backends consume the builder's output
hypervisor-neutrally:

1. **Disk prep** — :meth:`Builder.prepare_install_domain` returns the
   :class:`InstallDomain` spec (primary disk, optional seed ISO, extra
   CD-ROMs, and firmware hints).
2. **Post-install caching** — :meth:`Builder.install_manifest` populates
   the per-VM ``manifest.json`` stored alongside the cached primary
   disk under ``<cache_root>/vms/<config_hash>/``.
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
    from testrange.cache import CacheManager
    from testrange.communication.base import AbstractCommunicator
    from testrange.vms.base import AbstractVM as VM


@dataclass(frozen=True)
class InstallDomain:
    """Instructions a backend needs to build the install-phase domain.

    All disk-like fields are **backend-local refs** — strings that the
    hypervisor's host can open directly.  For the default
    :class:`~testrange.storage.LocalStorageBackend` these are absolute
    outer-host paths; for remote backends they are paths on the remote
    host where the uploaded disks live.

    :param work_disk: Backend-local ref to the primary disk the
        install domain will boot and write to.  Cloud-init uses an
        overlay on the resolved base image; autounattend uses a blank
        disk.
    :param seed_iso: Optional backend-local ref to the seed CD-ROM
        (cloud-init or autounattend seed) attached as the first
        CD-ROM device.
    :param extra_cdroms: Additional CD-ROM refs, in attach order after
        the seed.  Used by the Windows install flow to surface the
        install ISO and the driver disc.
    :param uefi: If ``True``, the backend should boot the install
        domain in UEFI mode; otherwise BIOS.  Windows GPT installs
        require UEFI.
    :param windows: If ``True``, the backend should use device models
        compatible with stock Windows Setup (which lacks drivers for
        the more modern paravirt devices).
    :param boot_cdrom: If ``True``, boot from CD-ROM before disk — the
        Windows installer needs this; cloud-init boots straight off
        the disk.
    """

    work_disk: str
    seed_iso: str | None = None
    extra_cdroms: tuple[str, ...] = ()
    uefi: bool = False
    windows: bool = False
    boot_cdrom: bool = False


@dataclass(frozen=True)
class RunDomain:
    """Instructions a backend needs to build the run-phase domain.

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
    """Strategy for provisioning a VM.

    Concrete implementations live in :mod:`testrange.vms.builders`.
    Subclass this to support a new install mechanism (preseed,
    Kickstart, Ignition, sysprep'd Windows, etc.).

    Builders are stateless: everything they need to know about a
    specific VM is passed in through ``vm`` or computed from the
    :class:`~testrange.cache.CacheManager` / :class:`~testrange._run.RunDir`
    argument.  One builder instance can safely serve many VMs, across
    any backend.
    """

    @abstractmethod
    def default_communicator(self) -> str:
        """Return the default communicator kind for VMs using this builder.

        Chosen by the backend's VM class when the caller does not pass
        ``communicator=``.  Typical values: ``"guest-agent"``,
        ``"ssh"``, ``"winrm"``.
        """

    def needs_install_phase(self) -> bool:
        """Whether to boot a one-off install-phase domain for this VM.

        Default is ``True``; :class:`NoOpBuilder`-style implementations
        override to ``False``.  When ``False``, the backend's
        ``build()`` calls :meth:`ready_image` instead of going through
        the install flow.
        """
        return True

    def needs_boot_keypress(self) -> bool:
        """Whether the install domain needs spacebars spammed at boot.

        Some install media (notably Windows install ISOs under UEFI)
        show a time-limited "Press any key to boot from CD or DVD..."
        prompt.  A VM has no physical keyboard to press it, so the
        prompt times out, UEFI falls through to the empty hard disk,
        and then drops to the firmware shell.  Builders that need
        boot keypresses override this to ``True`` and the backend
        spawns a short-lived thread to deliver key events during the
        early boot window.

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

    def has_post_install_hook(self) -> bool:
        """Whether this builder needs a re-boot for :meth:`post_install_hook`.

        The orchestrator's install path checks this before re-starting
        the just-installed VM to run the hook.  Re-booting the install
        VM is a 30-60s round trip on a cache miss; skipping it for
        builders with the default no-op hook keeps the cache-hit path
        unchanged for cloud-init / NoOp / Windows builds.

        Default is ``False``.  Override to ``True`` whenever
        :meth:`post_install_hook` is overridden — the orchestrator
        otherwise won't call your hook.
        """
        return False

    def post_install_hook(
        self,
        vm: VM,
        communicator: AbstractCommunicator,
    ) -> None:
        """Run setup commands inside the installed system before the
        cache snapshot is taken.

        Called by the orchestrator on a freshly **re-booted** install VM
        — the install phase ended in a clean poweroff (cloud-init's
        ``power_state: poweroff`` or PVE answer.toml's
        ``reboot-mode = "power-off"``), then the orchestrator restarts
        the same VM so this hook can SSH in.  After the hook returns,
        the orchestrator issues a clean shutdown and snapshots the
        result.

        Default is a no-op.  Override to bake setup state (extra
        package installs, repository swaps, persistent config files)
        into the cached install artifact, so the run phase doesn't
        depend on internet connectivity from the run-phase network.

        :param vm: The VM spec being built.
        :param communicator: A live :class:`AbstractCommunicator`
            attached to the install VM on the install network.
        :raises Exception: Anything raised by the hook propagates up;
            the orchestrator treats it as an install-phase failure.

        .. note::

           Implementations must contribute a deterministic digest of
           any script body or input they consume to
           :meth:`post_install_cache_key_extra` so cached install
           artifacts are invalidated when the hook changes.  Without
           it, an old cached artifact would silently survive a hook
           edit and the bug it was supposed to fix would persist.
        """
        del vm, communicator  # default no-op

    def post_install_cache_key_extra(self, vm: VM) -> str:
        """Return an extra string folded into the install cache key.

        Default is ``""`` (no contribution).  Overriders that
        implement :meth:`post_install_hook` return a deterministic
        digest of the hook's input (typically a SHA-256 prefix of the
        script body) so any edit to what the hook does invalidates
        every cached install artifact built with the previous version.

        Folded into :meth:`cache_key` by concrete implementations that
        need it; the ABC does not assume a specific cache-key shape.
        """
        del vm
        return ""

    def preferred_install_format(self) -> str:
        """Return the disk format this builder's install phase
        produces.

        Default is ``"qcow2"`` — today's universal answer because
        every shipped backend (libvirt, Proxmox) consumes qcow2
        natively and every shipped builder produces it.  Override
        only when adding a builder whose install pipeline outputs
        something else (a hypothetical Windows-on-ESXi builder
        emitting vmdk directly, for example).

        Used by :meth:`adopt_prebuilt` overrides to decide whether a
        :class:`~testrange._disk_format.DiskFormatConverter` is
        needed before importing into the inner backend.  Slice 4
        scaffolding — fully wired when the first non-qcow2 backend
        lands.
        """
        return "qcow2"

    def adopt_prebuilt(
        self,
        vm: VM,
        prebuilt_ref: str,
        run: RunDir,
        cache: CacheManager,
    ) -> str:
        """Take a *prebuilt_ref* (a disk built by an outer
        orchestrator's install loop) and return a backend-local ref
        the run phase can boot — without re-running the install.

        Called by an inner orchestrator's ``vm.build()`` when a nested
        topology has already produced the installed disk on the bare-
        metal cache.  *prebuilt_ref* points at the outer-host artifact
        (typically ``<outer_cache>/vms/<config_hash>/disk.qcow2``);
        this method moves / imports / clones it into whatever shape
        the inner backend needs to boot.

        Default raises :class:`NotImplementedError` — mirrors
        :meth:`ready_image`'s "you should have checked the phase
        indicator first" pattern.  Builders that support the
        nested-import path override this; backends that don't yet
        support nested-import keep the default and the inner
        orchestrator falls back to its own install path.

        :param vm: The VM spec being adopted.  Used for naming and
            cache-key derivation by overrides.
        :param prebuilt_ref: Source-side reference to the bare-metal-
            built disk.  Format depends on the outer orchestrator's
            storage backend (typically a filesystem path).
        :param run: Per-run scratch dir on the *inner* backend.
            ``run.storage.transport`` is how to put bytes onto the
            inner side.
        :param cache: Inner-orchestrator's :class:`CacheManager` for
            destination-side ref construction.
        :returns: An inner-backend-local ref to the run-ready disk.
        :raises NotImplementedError: Default — no override.
        """
        del vm, prebuilt_ref, run, cache
        raise NotImplementedError(
            f"{type(self).__name__}.adopt_prebuilt() is not implemented; "
            "the inner orchestrator should fall back to its own "
            "install-phase path or fail loud."
        )


__all__ = ["Builder", "InstallDomain", "RunDomain"]
