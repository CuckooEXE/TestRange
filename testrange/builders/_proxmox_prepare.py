"""Prepare a Proxmox VE installer ISO for unattended installation (ADR-0022).

Activation (verified against PVE 9.x ``proxmox-fetch-answer``): the installer
reads ``/auto-installer-mode.toml`` at the root of the booted ISO. If present,
it skips the interactive TUI and fetches ``answer.toml`` per the declared mode
(here ``partition`` — off the seed ISO labelled ``PROXMOX-AIS``). If absent, it
drops to interactive mode. Preparation is therefore one operation: add that file
(and the ``/proxmox-first-boot`` script) at the ISO root.

The catch is that the PVE installer ISO is **hybrid** — its UEFI boot path
depends on a precise El Torito + GPT/MBR/HFS+ layout. A pure-``pycdlib``
rebuild (``open`` → ``add_fp`` → ``write_fp``) preserves only the basic El
Torito boot record and strips the hybrid GPT, so the prepared ISO boots to
``grub>`` instead of the installer — reproduced every time in the prior impl.

We therefore drive ``xorriso`` with ``-boot_image any keep``, which preserves
the original boot infrastructure byte-for-byte while appending the new files.
This is a sanctioned ``subprocess`` use (ADR-0022, shared with the ESXi prep
module ``_esxi_prepare.py``); the project-wide ban (ADR-0001) carves out exactly
those two modules. ``xorriso`` ships with libisoburn on every mainstream distro.

PVE 8.x used a different activation mechanism (the mode file lived inside the
installer initrd), so this module is intentionally PVE 9.x-shaped.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import BuilderError

_log = get_logger(__name__)

_AUTO_INSTALLER_MODE_TOML = 'mode = "partition"\npartition_label = "{label}"\n'
"""ISO-root activation file. Field names per PVE 9.x ``utils.rs::AutoInstSettings``
— ``partition_label`` is **underscored**; the docs' hyphenated form is a parse
error."""


class ProxmoxPrepareError(BuilderError):
    """Raised when preparing a Proxmox installer ISO fails.

    Covers a missing ``xorriso`` binary, a corrupted vanilla input, or any
    non-zero exit from the ``xorriso`` process.
    """


def prepare_iso(
    vanilla_iso: Path,
    out_path: Path,
    *,
    partition_label: str,
    first_boot_script: str,
) -> None:
    """Write an auto-install-enabled copy of *vanilla_iso* to *out_path*.

    Drives ``xorriso -indev VANILLA -outdev OUT -boot_image any keep -map TOML
    /auto-installer-mode.toml -map SCRIPT /proxmox-first-boot -commit``. The
    ``-boot_image any keep`` flag preserves the El Torito + hybrid GPT/MBR/HFS+
    infrastructure so PVE's UEFI GRUB still finds its EFI System Partition.

    Args:
      vanilla_iso: Existing PVE installer ISO on disk.
      out_path: Where to write the prepared ISO. Overwritten if present.
      partition_label: Volume label the prepared installer searches for at
        install time to read ``answer.toml`` (matches the seed ISO's label).
      first_boot_script: Bash body embedded at ``/proxmox-first-boot`` (PVE's
        auto-installer copies it into the installed system and runs it as a
        oneshot via ``[first-boot] source = "from-iso"``). Marked ``0o755`` so
        the ``Type=oneshot`` ExecStart doesn't fail with EACCES.

    Raises:
      ProxmoxPrepareError: ``xorriso`` missing on ``$PATH``, the vanilla ISO
        cannot be opened, or ``xorriso`` returns non-zero.
    """
    vanilla_iso = vanilla_iso.expanduser().resolve()
    out_path = out_path.expanduser().resolve()

    xorriso = shutil.which("xorriso")
    if xorriso is None:
        raise ProxmoxPrepareError(
            "xorriso not found on $PATH — install it with `apt install xorriso` "
            "(Debian/Ubuntu), `dnf install xorriso` (Fedora/RHEL), or `brew install "
            "xorriso` (macOS). Required to preserve the PVE installer ISO's hybrid "
            "UEFI boot setup while injecting /auto-installer-mode.toml (ADR-0022)."
        )

    _log.info(
        "preparing PVE ISO %s -> %s (partition_label=%s)",
        vanilla_iso,
        out_path,
        partition_label,
    )
    with tempfile.TemporaryDirectory(prefix="testrange-pve-prep-") as td:
        mode_toml = Path(td) / "auto-installer-mode.toml"
        mode_toml.write_text(_AUTO_INSTALLER_MODE_TOML.format(label=partition_label))
        first_boot = Path(td) / "proxmox-first-boot"
        first_boot.write_text(first_boot_script)
        # xorriso -map preserves source filesystem permissions; 0o755 so PVE's
        # oneshot ExecStart can execute the script.
        first_boot.chmod(0o755)
        cmd = [
            xorriso,
            # Lift the exit threshold above the benign post-write SORRY: the
            # original protective MBR encoded the original image size, and the
            # image (grown by two files) is now slightly smaller than that entry
            # implies. Not a write failure — the "Writing ... completed
            # successfully" line precedes it — and the ISO is still bootable on
            # all three paths (UEFI/GPT, BIOS/El-Torito, hybrid-USB/MBR boot
            # code). Real write errors still exit non-zero (FAILURE/FATAL).
            "-return_with",
            "FAILURE",
            "32",
            "-indev",
            str(vanilla_iso),
            "-outdev",
            str(out_path),
            "-boot_image",
            "any",
            "keep",
            "-map",
            str(mode_toml),
            "/auto-installer-mode.toml",
            "-map",
            str(first_boot),
            "/proxmox-first-boot",
            "-commit",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise ProxmoxPrepareError(
                f"xorriso prepare-iso failed (exit {exc.returncode}): {stderr or '(no stderr)'}"
            ) from exc
        except FileNotFoundError as exc:  # pragma: no cover - racy with which()
            raise ProxmoxPrepareError(
                f"xorriso disappeared between which() and exec(): {exc}"
            ) from exc


__all__ = ["ProxmoxPrepareError", "prepare_iso"]
