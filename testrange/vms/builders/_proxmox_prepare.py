"""Prepare a ProxMox VE installer ISO for unattended installation.

Activation mechanism (verified against PVE 9.x's
``proxmox-fetch-answer`` source at
``proxmox-fetch-answer/src/main.rs``): the installer's squashfs
contains an entry-point that reads ``/cdrom/auto-installer-mode.toml``
(the mounted-ISO path for the root of the prepared installer ISO).
If that file is present, the installer skips the interactive TUI and
fetches ``answer.toml`` per the mode declared in the file
(``iso`` / ``http`` / ``partition``).  If absent, the installer
drops to interactive mode.

Preparation is therefore one operation: add ``auto-installer-mode.toml``
at the ISO root.  The catch is that the PVE installer ISO is a
**hybrid** image — its UEFI boot path depends on a precise El Torito
+ GPT/MBR/HFS+ layout that's far more involved than a vanilla
ISO9660 filesystem.  PVE's UEFI GRUB has an embedded prefix-finding
config that walks the GPT to locate the EFI System Partition, and
without that GPT entry GRUB drops to its interactive ``grub>`` shell
instead of loading ``/boot/grub/grub.cfg``.

We therefore drive ``xorriso`` (libisoburn's CLI) with
``-boot_image any keep`` so the original boot setup is preserved
byte-for-byte while the new file is appended.  An earlier pure-Python
implementation using ``pycdlib.write_fp()`` only preserved the basic
El Torito boot record and stripped the hybrid GPT/MBR/HFS+
infrastructure — same symptom every time: the prepared ISO booted
to ``grub>``, never to the installer.

xorriso is a system dependency.  It ships with libisoburn on every
mainstream Linux distro (``apt install xorriso`` on Debian/Ubuntu;
already in ``buildah``, ``ostree``, and the Proxmox toolchain).

PVE 8.x used a different activation mechanism (the mode file lived
*inside* the installer initrd), so this module is intentionally
PVE 9.x-shaped.  Adding 8.x support would mean version-detecting the
ISO and branching the prep strategy.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from testrange._logging import get_logger, log_duration
from testrange.exceptions import TestRangeError

_log = get_logger(__name__)

_PARTITION_LABEL_DEFAULT = "PROXMOX-AIS"
"""Volume label the prepared installer searches for in
``fetch-from = partition`` mode.  Matched case-insensitively by
``proxmox-fetch-answer``, so emitting uppercase here is safe whether
the seed ISO's volume identifier ends up upper- or lowercase."""

_AUTO_INSTALLER_MODE_TOML = """\
mode = "partition"
partition_label = "{label}"
"""
"""Template for the ISO-root mode file.

Field names come from PVE 9.x's
``proxmox-auto-installer/src/utils.rs::AutoInstSettings`` —
``partition_label`` is underscored; the docs' hyphenated form
(``partition-label``) is a parsing error.
"""


class ProxmoxPrepareError(TestRangeError):
    """Raised when preparing a ProxMox installer ISO fails.

    Covers a missing ``xorriso`` binary, a corrupted vanilla input,
    or any non-zero exit from the xorriso subprocess.
    """


def prepare_iso_bytes(
    vanilla_iso_path: Path,
    out_path: Path,
    *,
    partition_label: str = _PARTITION_LABEL_DEFAULT,
    first_boot_script: str | None = None,
) -> None:
    """Write an auto-install-enabled copy of *vanilla_iso_path* to *out_path*.

    Drives ``xorriso -indev VANILLA -outdev OUT -boot_image any keep
    -map TOML /auto-installer-mode.toml [-map SCRIPT
    /proxmox-first-boot] -commit``: the ``-boot_image any keep``
    flag preserves the original El Torito + hybrid GPT/MBR/HFS+
    infrastructure so PVE's UEFI GRUB can still find the EFI System
    Partition the way it expects.

    :param vanilla_iso_path: Existing PVE installer ISO on disk.
    :param out_path: Where to write the prepared ISO.  Must not exist
        yet (the caller handles caching / locking).
    :param partition_label: Volume label the prepared installer
        searches for at install time to read ``answer.toml``.
        Defaults to ``PROXMOX-AIS``.
    :param first_boot_script: Optional bash script body to embed at
        ``/proxmox-first-boot`` on the prepared ISO — the path PVE's
        ``proxmox-fetch-answer`` looks at when ``answer.toml`` carries
        ``[first-boot] source = "from-iso"``.  Mirrors the upstream
        ``proxmox-auto-install-assistant prepare-iso --on-first-boot
        SCRIPT`` flag.  When ``None``, the [first-boot] field on the
        ISO is left empty (PVE skips the hook).
    :raises ProxmoxPrepareError: When ``xorriso`` is missing on
        ``$PATH``, when the vanilla ISO can't be opened, or when
        ``xorriso`` itself returns non-zero.
    """
    vanilla_iso_path = vanilla_iso_path.expanduser().resolve()
    out_path = out_path.expanduser().resolve()

    xorriso_bin = shutil.which("xorriso")
    if xorriso_bin is None:
        raise ProxmoxPrepareError(
            "xorriso not found on $PATH — install it with "
            "``apt install xorriso`` (Debian/Ubuntu), ``dnf install "
            "xorriso`` (Fedora/RHEL), or ``brew install xorriso`` "
            "(macOS).  Required to preserve the PVE installer ISO's "
            "hybrid UEFI boot setup while injecting "
            "/auto-installer-mode.toml."
        )

    _log.info(
        "preparing PVE ISO %s → %s (partition_label=%s, first-boot=%s)",
        vanilla_iso_path, out_path, partition_label,
        "yes" if first_boot_script else "no",
    )

    toml_body = _AUTO_INSTALLER_MODE_TOML.format(
        label=partition_label,
    )

    # xorriso's ``-map LOCAL ISO`` takes a real filesystem path, so
    # stage every payload (TOML body, optional first-boot script) in
    # temp files for the duration of the subprocess call.
    tmp_paths: list[Path] = []
    with tempfile.NamedTemporaryFile(
        prefix="testrange-auto-installer-mode-",
        suffix=".toml",
        delete=False,
    ) as tmp:
        tmp.write(toml_body.encode("utf-8"))
        toml_tmp_path = Path(tmp.name)
        tmp_paths.append(toml_tmp_path)

    script_tmp_path: Path | None = None
    if first_boot_script is not None:
        with tempfile.NamedTemporaryFile(
            prefix="testrange-proxmox-first-boot-",
            suffix=".sh",
            delete=False,
        ) as tmp:
            tmp.write(first_boot_script.encode("utf-8"))
            script_tmp_path = Path(tmp.name)
            tmp_paths.append(script_tmp_path)

    try:
        cmd = [
            xorriso_bin,
            # ``-return_with FAILURE 32``: lift the exit-code
            # threshold from xorriso's default ``SORRY`` so the
            # post-write re-assessment doesn't fail us when it
            # spots that the *original* ISO's protective MBR
            # encoded the *original* image size and the new
            # image, having grown by one file, is now slightly
            # smaller than the MBR's partition entry implies.
            # That's a libburn SORRY about MBR metadata, not a
            # write failure (the "Writing to ... completed
            # successfully" line precedes it) and the resulting
            # ISO is still bootable: UEFI uses the GPT, BIOS uses
            # the El Torito catalog, and hybrid-USB boot uses the
            # MBR's *boot code* offset — none of those care about
            # the MBR partition entry's encoded size.  Real
            # write-side problems still surface as FAILURE or
            # FATAL events and exit non-zero.
            "-return_with", "FAILURE", "32",
            "-indev", str(vanilla_iso_path),
            "-outdev", str(out_path),
            # ``any keep`` preserves the original El Torito catalog,
            # MBR/GPT hybrid layout, and EFI System Partition pointer
            # exactly — which PVE's UEFI GRUB depends on for
            # locating its own ``grub.cfg``.
            "-boot_image", "any", "keep",
            "-map", str(toml_tmp_path), "/auto-installer-mode.toml",
        ]
        if script_tmp_path is not None:
            # ``/proxmox-first-boot`` is the literal path
            # ``proxmox-auto-install-assistant prepare-iso
            # --on-first-boot`` writes — verified against the
            # ``proxmox-first-boot`` string baked into the
            # proxmox-auto-install-assistant binary at
            # ``/usr/bin/proxmox-auto-install-assistant``.  Anything
            # else and PVE's installer logs "Failed loading
            # first-boot executable from iso (was iso prepared with
            # --on-first-boot)" and aborts the install.
            cmd += ["-map", str(script_tmp_path), "/proxmox-first-boot"]
        cmd += ["-commit"]
        with log_duration(_log, "write prepared PVE ISO"):
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                # xorriso writes diagnostics to stderr; bubble them up
                # so a corrupted vanilla ISO or a libisoburn quirk is
                # immediately obvious in the test runner's log.
                stderr = (exc.stderr or "").strip()
                raise ProxmoxPrepareError(
                    f"xorriso prepare-iso failed (exit {exc.returncode}): "
                    f"{stderr or '(no stderr)'}"
                ) from exc
            except FileNotFoundError as exc:  # pragma: no cover — racy with shutil.which
                raise ProxmoxPrepareError(
                    f"xorriso disappeared between which() and exec(): {exc}"
                ) from exc
    finally:
        for path in tmp_paths:
            try:
                path.unlink()
            except OSError:
                pass


__all__ = [
    "ProxmoxPrepareError",
    "prepare_iso_bytes",
]
