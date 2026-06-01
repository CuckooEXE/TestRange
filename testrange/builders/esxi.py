"""ESXiKickstartBuilder — kickstart-driven unattended ESXi 8 install.

Installs an ESXi node *as a guest* (nested-virt labs) via weasel's kickstart.
Installer-origin (BUILD-1): ``os_disk_base()`` is None and ``boot_media()`` is
the ESXi installer ISO. **Single-CDROM mode** — unlike the cloud-init/PVE
builders, ``render_seed()`` returns None: the ks.cfg is patched *into* the boot
ISO (``ks=cdrom:/ks.cfg``) by :meth:`prepare_boot_media`, not delivered as a
separate seed. The build-result contract lives in the kickstart's ``%firstboot``
block (serial ``TESTRANGE-RESULT`` + poweroff, ADR-0012).

Firmware comes from :attr:`VMSpec.firmware`. The proven-working combo is BIOS +
i440fx + IDE single-CDROM; ``uefi`` is accepted but UNVALIDATED (ESXi 8 + OVMF +
AHCI hits a "late-filesystems jumpstart plugin activation failed" event, and
Secure Boot needs VMware's signed bootloader CA pre-installed in OVMF). The
installer needs a system disk >= ~33 GiB — size the VM's ``OSDrive`` to >= 33.

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

# ESXi 8 wants ~33 GiB for the system partitions; below this the install fails to
# space-allocate. Surfaced as a fail-loud guard rather than a silent round-up.
_MIN_OS_DISK_GB = 33


class ESXiKickstartBuilder(Builder):
    """Kickstart-driven ESXi installer (installer-origin).

    Args:
      installer_iso: the **vanilla** ESXi installer ISO (a CacheEntry). The
        builder patches it (xorriso, ADR-0022) into a kickstart ISO during
        staging; this entry's content sha keys the cache.
      credentials: baked into the install. MUST include a ``root`` PosixCred
        with a non-empty password (ESXi 8 complexity rules reject an empty one).
        The root credential's SSH key, if present, is installed to
        ``/etc/ssh/keys-root/authorized_keys`` and sshd is enabled.
    """

    def __init__(
        self,
        *,
        installer_iso: CacheEntry,
        credentials: Sequence[Credential] = (),
    ) -> None:
        self.installer_iso = installer_iso
        self._credentials = self._validate_credentials(credentials)

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
                "password (ESXi 8 password-complexity rules reject an empty rootpw)"
            )
        usernames = [c.username for c in creds]
        dupes = {u for u in usernames if usernames.count(u) > 1}
        if dupes:
            raise ValueError(
                f"ESXiKickstartBuilder.credentials has duplicate usernames: {sorted(dupes)}"
            )
        return creds

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
        different system), the SSH-block *presence* (it flips ``%firstboot``;
        the key *value* is excluded so rotation does not bust the cache, per the
        PVE builder), the install disk size, ``spec.firmware``, and ``base_sha``
        (the vanilla ISO sha). Pure: no clocks/run_id/I/O (ADR-0007).
        """
        del recipe, addressing, macs, build_nic
        if spec.os_drive.size_gb < _MIN_OS_DISK_GB:
            raise ValueError(
                f"ESXiKickstartBuilder needs an OSDrive >= {_MIN_OS_DISK_GB} GiB "
                f"(ESXi 8 system partitions); got {spec.os_drive.size_gb}"
            )
        root = self._root_credential()
        has_ssh = root.ssh_key is not None
        combined = (
            f"root-password:{root.password}\n---\nssh-block:{has_ssh}\n---\n"
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
        ssh_key = root.ssh_key.auth_line if root.ssh_key is not None else None
        return render_kickstart(root_password=root.password or "", ssh_key=ssh_key)

    def _root_credential(self) -> PosixCred:
        for c in self._credentials:
            if c.username == "root" and isinstance(c, PosixCred):
                return c
        raise ValueError("no root credential")  # pragma: no cover — __init__ validates


__all__ = ["ESXiKickstartBuilder"]
