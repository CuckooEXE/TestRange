"""Builder ABC.

The Builder drives the install lifecycle end to end: it produces a
self-terminating install payload, and the orchestrator reads back an explicit
result over the guest's serial console.

**Build-result contract (ADR §21).** Every Builder MUST render provisioning
that (a) runs **fail-fast** — the first failing step aborts the rest, (b)
emits a framed ``TESTRANGE-RESULT:`` record to the guest's serial console,
and (c) powers the guest off. The positive ``ok`` token is the *only* success
signal the orchestrator accepts; a guest that powers off without it is treated
as a crashed build, never a cached disk. This contract lives *above* the
Builder ABC so it fits every dialect — cloud-init ``runcmd``, ESXi Kickstart
``%post``, Windows ``SetupComplete.cmd`` — each concrete renders it natively::

    TESTRANGE-RESULT: ok
    # --- or, on failure ---
    TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"
    TESTRANGE-LOG-BEGIN
    <base64 of the relevant log>
    TESTRANGE-LOG-END

The record goes to the **serial console only** (the most portable virtual
device); the driver's build-result sink hides the per-backend host-side read.

Builders are hypervisor-agnostic. When a builder needs per-network
addressing facts (CIDR, prefix, gateway, DHCP flag) to render guest config,
the orchestrator brokers: it builds a
``Mapping[network_name, NetworkAddressing]`` from
``hypervisor.all_networks`` and hands it in. The Builder never sees the
hypervisor type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.cache.entry import CacheEntry
    from testrange.credentials.base import Credential
    from testrange.guest_io import GuestExec
    from testrange.networks.base import BuildNic, NetworkAddressing
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


class Builder(ABC):
    """Abstract builder. Concretes own the install lifecycle."""

    @property
    @abstractmethod
    def credentials(self) -> tuple[Credential, ...]:
        """Credentials baked into the disk by this builder.

        Returned in declaration order. The orchestrator consults this when
        binding a Communicator that names a credential by username.
        """

    @abstractmethod
    def os_disk_base(self) -> CacheEntry | None:
        """The cache entry that seeds the VM's OS disk, or ``None``.

        This is the OS-disk *origin* the build phase uses: the orchestrator
        resolves the returned :class:`CacheEntry`, uploads its bytes onto the
        VM's own disk ref, and grows it. An image-based builder (cloud-init)
        returns its base image. ``None`` means the builder materializes its own
        OS disk — an installer-based origin (PVE auto-install, ESXi Kickstart,
        Windows autounattend) that boots blank media. In that case the builder
        returns the install medium from :meth:`boot_media`, and the orchestrator
        creates a blank OS disk of the declared size and boots the medium
        (BUILD-1, ADR-0010 §6). A builder that returns ``None`` here MUST return
        a non-``None`` :meth:`boot_media`; the orchestrator rejects a builder
        that provides neither at preflight.

        Abstract because OS-disk origin is a fundamental build property every
        builder must declare — the orchestrator reads it through this seam
        rather than knowing any concrete builder type.
        """

    def boot_media(self) -> CacheEntry | None:
        """The bootable install medium, or ``None`` (BUILD-1a, ADR-0010 §6).

        Default ``None`` — an image-based builder (cloud-init) seeds its OS disk
        from :meth:`os_disk_base` and needs no boot medium. An *installer-based*
        builder returns ``None`` from :meth:`os_disk_base` and the install ISO
        here: the orchestrator then materializes a **blank** OS disk of the
        declared size and boots this medium (attached as a bootable CDROM, the
        OS disk falling through to it while empty) so the installer partitions
        the disk unattended.

        The returned :class:`CacheEntry`'s content sha is folded into the build
        cache key (the orchestrator passes it as ``config_hash``'s ``base_sha``),
        so a different installer ISO invalidates the cache like a different base
        image would. Non-abstract: only installer-origin builders override it.
        """
        return None

    def prepare_boot_media(self, media_path: Path) -> Path:
        """Transform the resolved boot medium before it is staged, if needed.

        The orchestrator resolves :meth:`boot_media` to a local file and passes
        its path here; the returned path is what gets uploaded and booted.
        Default identity — most installer media boots as-is. An installer-origin
        builder whose installer needs an activation payload baked into the booted
        ISO overrides this to return a transformed copy.

        Called once per build miss (never on a cache hit), with the same resolved
        ``media_path`` each time — the orchestrator does **not** memoize across
        misses, so a builder whose transform is expensive (an ISO rewrite) owns
        its own caching, keyed however it likes. The vanilla medium's content sha
        already keys the build cache via ``config_hash``'s ``base_sha``; a
        transform that varies the installed system must fold its inputs into
        ``config_hash`` itself, so the orchestrator need not content-address the
        returned path.
        """
        return media_path

    @abstractmethod
    def config_hash(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        base_sha: str = "",
        sidecar_sha: str = "",
        macs: Sequence[str] = (),
        build_nic: BuildNic,
    ) -> str:
        """16-char hex hash that uniquely identifies the VM's built disk set.

        Pure and deterministic: same ``(spec, recipe, addressing, base_sha,
        sidecar_sha, macs, build_nic)`` -> same hash, every time, with no
        ``run_id``/clock/random input. This is the build cache key; the
        rationale and the contract for builder authors live in ADR-0007.

        ``base_sha`` is the OS-disk base image's content sha (from
        :meth:`os_disk_base`); ``sidecar_sha`` is the build sidecar image's
        content sha — every build boots on a sidecar-served switch, so a
        drifted sidecar must invalidate the cache. ``macs`` (one per NIC in
        spec order) lets concretes that bake positional NIC config into the
        install payload key the cache on the stable MACs the orchestrator will
        assign at run-phase. ``build_nic`` is the dedicated build NIC the build
        phase attaches (ADR-0017); its MAC/address are baked into the netplan,
        so they are part of the key.
        """

    @abstractmethod
    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
        build_nic: BuildNic,
    ) -> bytes | None:
        """Render the install payload (e.g., a cloud-init seed ISO) as bytes.

        Return ``None`` when this builder needs no seed medium at all — a
        builder that produces a fully-baked disk with nothing to hand the guest
        at boot (the build phase then attaches no seed ISO). A concrete that
        always emits a seed narrows its own return type to ``bytes``.

        When a seed *is* produced, then per the build-result contract (module
        docstring) the rendered payload MUST run fail-fast, emit the framed
        ``TESTRANGE-RESULT:`` record to the guest serial console, and power off.

        ``macs`` (one per NIC in spec order) lets concretes bake positional NIC
        config (the match-by-MAC netplan) into the payload. ``build_nic`` is the
        dedicated build NIC the build phase attaches in place of the declared
        NICs (ADR-0017) — the install boot egresses through it.
        """

    def wait_ready(self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec) -> None:
        """Block until the brought-up VM is ready for tests.

        Default: no-op — for builders that produce a fully-baked disk
        with no post-boot finalization. Concretes whose build leaves
        work to finish at run-phase boot (cloud-init's stage machine,
        Ignition's finalize, etc.) override: run the readiness command
        via ``execute`` and raise :class:`BuildNotReadyError` if it
        never succeeds. The builder never sees a Communicator type —
        only the injected ``execute`` callable. The orchestrator calls
        this after ``_bind_communicators`` and before yielding the
        ``OrchestratorHandle`` to test code.
        """
        del spec, recipe, execute  # real statement: a docstring-only body trips B027
