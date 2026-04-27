"""Prepare a ProxMox VE installer ISO for unattended installation.

Activation mechanism (verified against PVE 9.x's
`proxmox-fetch-answer` source at
``proxmox-fetch-answer/src/main.rs``): the installer's squashfs
contains an entry-point that reads ``/cdrom/auto-installer-mode.toml``
(the mounted-ISO path for the root of the prepared installer ISO).
If that file is present, the installer skips the interactive TUI and
fetches ``answer.toml`` per the mode declared in the file
(``iso`` / ``http`` / ``partition``).  If absent, the installer
drops to interactive mode.

Preparation is therefore one operation: add ``auto-installer-mode.toml``
at the ISO root.  No initrd patching, no xorriso, no
``proxmox-auto-install-assistant`` binary — pure pycdlib.

PVE 8.x used a different mechanism (the mode file lived *inside* the
installer initrd), so this module is intentionally PVE 9.x-shaped.
Adding 8.x support would mean version-detecting the ISO and branching
the prep strategy.
"""

from __future__ import annotations

import io
from pathlib import Path

from pycdlib import PyCdlib  # type: ignore[attr-defined]

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

    Covers pycdlib open/write errors and anything else unexpected in
    the ISO-manipulation path.
    """


def prepare_iso_bytes(
    vanilla_iso_path: Path,
    out_path: Path,
    *,
    partition_label: str = _PARTITION_LABEL_DEFAULT,
) -> None:
    """Write an auto-install-enabled copy of *vanilla_iso_path* to *out_path*.

    Opens the vanilla PVE installer ISO with pycdlib, adds a small
    ``auto-installer-mode.toml`` at the ISO root, and writes a new
    ISO.  El Torito boot records — including the UEFI boot catalog
    entry — are preserved through pycdlib's standard write path.

    :param vanilla_iso_path: Existing PVE installer ISO on disk.
    :param out_path: Where to write the prepared ISO.  Must not exist
        yet (the caller handles caching / locking).
    :param partition_label: Volume label the prepared installer
        searches for at install time to read ``answer.toml``.
        Defaults to ``PROXMOX-AIS``.
    :raises ProxmoxPrepareError: On any pycdlib failure.
    """
    vanilla_iso_path = vanilla_iso_path.expanduser().resolve()
    out_path = out_path.expanduser().resolve()
    _log.info(
        "preparing PVE ISO %s → %s (partition_label=%s)",
        vanilla_iso_path, out_path, partition_label,
    )

    toml_body = _AUTO_INSTALLER_MODE_TOML.format(
        label=partition_label,
    ).encode()

    iso = PyCdlib()
    try:
        iso.open(str(vanilla_iso_path))
    except Exception as exc:  # noqa: BLE001
        raise ProxmoxPrepareError(
            f"failed to open PVE ISO {vanilla_iso_path}: {exc}"
        ) from exc

    try:
        # PVE 9.x ISOs ship Rock Ridge + ISO9660 (no Joliet).  The
        # installer reads `/cdrom/auto-installer-mode.toml` by Rock
        # Ridge name, so we must emit the Rock Ridge basename; the
        # ISO9660 alias (uppercase, ``;1``-suffixed) is required by
        # pycdlib's add_fp contract whenever the ISO has an ISO9660
        # namespace (always, in practice).
        add_kwargs: dict[str, object] = {
            "iso_path": "/AUTOINST.TOM;1",
        }
        if iso.has_rock_ridge():
            add_kwargs["rr_name"] = "auto-installer-mode.toml"
        if iso.has_joliet():
            add_kwargs["joliet_path"] = "/auto-installer-mode.toml"

        try:
            iso.add_fp(
                io.BytesIO(toml_body),
                len(toml_body),
                **add_kwargs,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001
            raise ProxmoxPrepareError(
                "failed to add auto-installer-mode.toml to ISO: "
                f"{exc}"
            ) from exc

        _log.debug(
            "added /auto-installer-mode.toml (%d bytes) to prepared ISO",
            len(toml_body),
        )

        with (
            log_duration(_log, "write prepared PVE ISO"),
            open(out_path, "wb") as fh,
        ):
            iso.write_fp(fh)
    finally:
        iso.close()


__all__ = [
    "ProxmoxPrepareError",
    "prepare_iso_bytes",
]
