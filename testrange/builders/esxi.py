"""ESXiKickstartBuilder — kickstart-driven unattended ESXi install (validated on 8).

Installs an ESXi node *as a guest* (nested-virt labs) via weasel's kickstart.
Installer-origin (BUILD-1): ``os_disk_base()`` is None and ``boot_media()`` is
the ESXi installer ISO. **Single-CDROM mode** — unlike the cloud-init/PVE
builders, ``render_seed()`` returns None: the ks.cfg is patched *into* the boot
ISO (``ks=cdrom:/ks.cfg``) by :meth:`prepare_boot_media`, not delivered as a
separate seed. The build-result contract is honored serial-side like every other
builder (ADR-0012), but ESXi has no userspace serial write: the kickstart's
``%post`` injects ``TESTRANGE-RESULT: ok`` into the installer vmkernel log via
``vsish`` (the log streams out COM1 via ``logPort=com1``) and powers the installer
off — see :func:`testrange.builders._esxi_prepare.render_kickstart`. ``%firstboot``
carries only run-phase provisioning (SSH key + sshd), which runs when the captured
disk is booted, not during the build.

Firmware comes from :attr:`VMSpec.firmware`. The proven-working combo is BIOS +
i440fx + IDE single-CDROM; ``uefi`` is accepted but UNVALIDATED (OVMF + AHCI hits
a "late-filesystems jumpstart plugin activation failed" event, and Secure Boot
needs VMware's signed bootloader CA pre-installed in OVMF). Size the VM's
``OSDrive`` for the installer's system partitions (~33 GiB on 8.x); the installer
itself fails loud on an undersized disk.

Distinct from the ESXi *driver* (BACKEND-2) and the VMware Tools communicator
(COMM-2): this builder only renders ks.cfg + patches the installer ISO; SSH (via
the root key the kickstart installs) is enough, so it does not depend on COMM-2.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from testrange.builders._esxi_prepare import prepare_iso, render_kickstart
from testrange.builders.base import Builder
from testrange.cache.entry import CacheEntry
from testrange.credentials.base import Credential
from testrange.credentials.posix import PosixCred
from testrange.exceptions import BuildNotReadyError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec
    from testrange.networks.base import BuildNic, NetworkAddressing
    from testrange.vms.recipe import VMRecipe
    from testrange.vms.spec import VMSpec


def _reject_control_chars(value: str, what: str) -> None:
    """Reject newlines/control chars that would break out of a ks.cfg directive."""
    bad = {c for c in value if c == "\n" or c == "\r" or ord(c) < 0x20}
    if bad:
        raise ValueError(
            f"ESXiKickstartBuilder {what} contains control characters {sorted(bad)!r} "
            "that would corrupt the generated kickstart"
        )


class ESXiKickstartBuilder(Builder):
    """Kickstart-driven ESXi installer (installer-origin).

    Args:
      installer_iso: the **vanilla** ESXi installer ISO (a CacheEntry). The
        builder patches it (xorriso, ADR-0022) into a kickstart ISO during
        staging; this entry's content sha keys the cache.
      credentials: baked into the install. MUST include a ``root`` PosixCred
        with a non-empty password (ESXi complexity rules reject an empty one).
        The root credential's SSH key, if present, is installed to
        ``/etc/ssh/keys-root/authorized_keys`` and sshd is enabled.
      license: optional ESXi license key. When set, the kickstart applies it at
        install time via ``serialnum --esx=<key>`` (the native weasel directive),
        so the installed node comes up licensed rather than on the read-only
        free/evaluation edition. ``None`` leaves the install on its default
        evaluation license. The key folds into :meth:`config_hash` — a different
        license is a different installed system.
    """

    def __init__(
        self,
        *,
        installer_iso: CacheEntry,
        credentials: Sequence[Credential] = (),
        license: str | None = None,
    ) -> None:
        self.installer_iso = installer_iso
        self._credentials = self._validate_credentials(credentials)
        self._license = self._validate_license(license)

    @staticmethod
    def _validate_credentials(credentials: Sequence[Credential]) -> tuple[Credential, ...]:
        creds = tuple(credentials)
        root = next((c for c in creds if c.username == "root"), None)
        if root is None:
            raise ValueError(
                "ESXiKickstartBuilder requires a root Credential (kickstart needs a rootpw)"
            )
        if not isinstance(root, PosixCred) or not root.password:
            raise ValueError(
                "ESXiKickstartBuilder's root Credential must be a PosixCred with a non-empty "
                "password (ESXi password-complexity rules reject an empty rootpw)"
            )
        # The password lands raw on the ks.cfg ``rootpw`` line and the SSH key in
        # a heredoc; a newline/control char would break out of the directive and
        # emit a malformed (or directive-injecting) kickstart. Not a security
        # boundary (lab guest install), but a footgun — reject at construction.
        _reject_control_chars(root.password, "root password")
        # ESXi 8's sshd runs in FIPS mode and SILENTLY rejects Ed25519 pubkeys
        # (CORE-63), so a node installed with one is unreachable over SSH at run
        # phase. Fail loud at construction with a fix, rather than mysteriously
        # later. RSA/ECDSA are FIPS-approved; EcdsaKey is the in-tree choice.
        if root.ssh_key is not None and root.ssh_key.algorithm == "ed25519":
            raise ValueError(
                "ESXiKickstartBuilder's root SSH key is Ed25519, which ESXi's FIPS-mode "
                "sshd silently rejects — use testrange.utils.EcdsaKey (P-256) or an RSA key"
            )
        for c in creds:
            if isinstance(c, PosixCred) and c.ssh_key is not None:
                _reject_control_chars(c.ssh_key.auth_line, f"{c.username!r} SSH key")
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"ESXiKickstartBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        return creds

    @staticmethod
    def _validate_license(license: str | None) -> str | None:
        """Reject an empty/control-char license; ``None`` (no license) is fine.

        The key lands raw on the ks.cfg ``serialnum --esx=`` line, so a newline or
        control char would break out of the directive (same footgun as the root
        password — a lab-install footgun, not a security boundary).
        """
        if license is None:
            return None
        _reject_control_chars(license, "license")
        if not license.strip():
            raise ValueError(
                "ESXiKickstartBuilder license must be a non-empty license key (or None to "
                "leave the install on its default evaluation license)"
            )
        return license

    @property
    def credentials(self) -> tuple[Credential, ...]:
        return self._credentials

    def os_disk_base(self) -> None:
        """Installer-origin: no base image. ESXi's ``install --firstdisk``
        consumes a blank disk and partitions it internally (BUILD-1)."""

    def boot_media(self) -> CacheEntry:
        """The vanilla ESXi installer ISO; patched into a kickstart ISO by
        :meth:`prepare_boot_media` during staging."""
        return self.installer_iso

    def prepare_boot_media(self, media_path: Path) -> Path:
        """Patch the kickstart into the installer ISO (xorriso two-pass), caching
        the prepared copy beside the vanilla and keyed by the kickstart digest.

        The kickstart depends only on the builder's credentials (root password +
        optional SSH key), so the prepared ISO keys on its digest.
        """
        kickstart = self._render_kickstart()
        digest = hashlib.sha256(kickstart.encode("utf-8")).hexdigest()[:16]
        prepared = media_path.parent / f"{media_path.stem}-esxi-{digest}.iso"
        if not prepared.exists():
            prepare_iso(media_path, prepared, kickstart=kickstart)
        return prepared

    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
        build_nic: BuildNic,
    ) -> None:
        """No separate seed (single-CDROM): the ks.cfg rides the boot media."""
        del spec, recipe, addressing, macs, build_nic

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
        """Deterministic 16-char hex hash keying the installed ESXi disk.

        Folds: the root password (baked into the install — a change is a
        different system), the root SSH **public key** (its ``auth_line``, baked
        into ``%firstboot``'s ``authorized_keys`` — a different key, or none, is a
        different installed system; CORE-64), the license key (baked at install
        via ``serialnum``), the install disk size, ``spec.firmware``, and
        ``base_sha`` (the vanilla ISO sha). Pure: no clocks/run_id/I/O (ADR-0007).

        The key is folded by value, NOT just presence: run VMs boot the cached
        disk with no re-seed (``seed_iso_ref=None``), so the baked key is the only
        ``authorized_keys`` there is — excluding it would let a plan with a
        different key cache-hit a disk it cannot log into.
        """
        del recipe, addressing, macs, build_nic
        root = self._root_credential()
        ssh_key = root.ssh_key.auth_line if root.ssh_key is not None else ""
        combined = (
            f"root-password:{root.password}\n---\nssh-key:{ssh_key}\n---\n"
            f"license:{self._license}\n---\n"
            f"disk:{spec.os_drive.size_gb}\n---\nfirmware:{spec.firmware}\n---\n"
            f"base:{base_sha}\n---\nsidecar:{sidecar_sha}"
        )
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def wait_ready(self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec) -> None:
        """Confirm the installed node answers over SSH (the kickstart enabled
        sshd + installed the root key); the build already ran provisioning."""
        del spec, recipe
        r = execute(("true",), timeout=300.0)
        if r.exit_code != 0:
            raise BuildNotReadyError(
                f"ESXi node not reachable over SSH (exit {r.exit_code}); stderr={r.stderr!r}"
            )

    def build_kickstart(self) -> str:
        """The rendered ks.cfg (public so tests/debuggers can inspect it)."""
        return self._render_kickstart()

    def _render_kickstart(self) -> str:
        root = self._root_credential()
        assert root.password is not None  # construction guarantees a non-empty root password
        ssh_key = root.ssh_key.auth_line if root.ssh_key is not None else None
        return render_kickstart(root_password=root.password, ssh_key=ssh_key, license=self._license)

    def _root_credential(self) -> PosixCred:
        for c in self._credentials:
            if c.username == "root" and isinstance(c, PosixCred):
                return c
        raise ValueError("no root credential")  # pragma: no cover — __init__ validates


__all__ = ["ESXiKickstartBuilder"]
