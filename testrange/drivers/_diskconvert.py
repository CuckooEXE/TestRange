"""Sanctioned ``qemu-img`` disk-format conversion at a driver boundary (CORE-2).

Canonical cache format is **qcow2**, cache-wide (decision A, 2026-06-01). A
backend whose on-disk format is not qcow2 — ESXi (``vmdk`` now), Hyper-V
(``vhdx`` later) — converts at its driver boundary, both directions, through
this module. The on-backend projection is **derived and ephemeral**: only the
qcow2 is content-addressed, never the vmdk (``qemu-img`` vmdk output is not
byte-deterministic).

This invokes [ADR-0001](../../docs/adr/0001-subprocess-ban.md)'s escape hatch
and is governed by [ADR-0024](../../docs/adr/0024-qemu-img-disk-conversion.md):
``subprocess`` is banned project-wide, but ``qemu-img`` is a host **binary**,
not a Python library, so there is no ``_import_<dep>()`` wheel to call. It is
therefore a host-binary dependency discovered via :func:`shutil.which`
(preflight gates its absence loud, not at conversion time — ESXI-9). The ruff
ban is lifted for this one file in ``pyproject.toml`` (TID251/S404/S603), and
``tests/unit/test_subprocess_ban.py`` whitelists exactly this module.

The argument vector is fixed and built only from internal data (no shell, no
user-interpolated flags), so the audit surface the ban protects stays one
module with one external command.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from testrange._log import get_logger
from testrange.exceptions import DriverError

_log = get_logger(__name__)

_QEMU_IMG = "qemu-img"


def qemu_img_path() -> str | None:
    """Absolute path to ``qemu-img`` on ``$PATH``, or ``None`` if absent."""
    return shutil.which(_QEMU_IMG)


def require_qemu_img() -> str:
    """Resolve ``qemu-img`` or raise a :class:`DriverError` with an install hint.

    Preflight calls this so an image-origin build on a non-qcow2 backend fails
    loud on the orchestrator host *before* any backend resource stands up,
    rather than mid-upload (ESXI-9).
    """
    path = qemu_img_path()
    if path is None:
        raise DriverError(
            "qemu-img not found on PATH; a non-qcow2 backend (ESXi vmdk) converts disks "
            "at its driver boundary (CORE-2). Install QEMU tools "
            "(apt install qemu-utils / dnf install qemu-img / brew install qemu)."
        )
    return path


def _run(argv: list[str]) -> None:
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise DriverError(
            f"qemu-img failed (exit {exc.returncode}): {stderr or '(no stderr)'}"
        ) from exc
    except FileNotFoundError as exc:  # pragma: no cover - racy with which()
        raise DriverError(f"qemu-img disappeared between which() and exec(): {exc}") from exc
    # ``capture_output`` already keeps qemu-img off the raw terminal (fd 2), so it
    # never corrupts the live dashboard; route any chatter (progress/warnings) it
    # wrote through logging at DEBUG rather than discarding it — every byte of
    # output goes through the rich logging stack (ADR-0029), nothing to fd 2.
    for label, stream in (("stderr", result.stderr), ("stdout", result.stdout)):
        text = (stream or "").strip()
        if text:
            _log.debug("qemu-img %s: %s", label, text)


def convert(
    src: Path,
    dst: Path,
    *,
    out_format: str,
    in_format: str | None = None,
    subformat: str | None = None,
) -> Path:
    """Convert ``src`` to ``dst`` in ``out_format``. Returns ``dst``.

    ``in_format`` pins the source format (defensive: ``qemu-img`` otherwise
    probes, and a probe is a small attack surface on untrusted images); pass it
    where the source format is known. ``subformat`` selects a container variant
    (e.g. ``streamOptimized`` / ``monolithicSparse`` for vmdk). ``dst``'s parent
    is created if needed; an existing ``dst`` is overwritten.
    """
    qemu_img = require_qemu_img()
    if not src.exists():
        raise DriverError(f"qemu-img convert: source {src!s} does not exist")
    dst.parent.mkdir(parents=True, exist_ok=True)
    argv = [qemu_img, "convert"]
    if in_format is not None:
        argv += ["-f", in_format]
    argv += ["-O", out_format]
    if subformat is not None:
        argv += ["-o", f"subformat={subformat}"]
    argv += [str(src), str(dst)]
    _log.info(
        "qemu-img convert %s -> %s (%s%s)",
        src.name,
        dst.name,
        out_format,
        f"/{subformat}" if subformat else "",
    )
    _run(argv)
    return dst


def qcow2_to_vmdk(src: Path, dst: Path, *, subformat: str = "streamOptimized") -> Path:
    """qcow2 → vmdk for ingest onto a non-qcow2 backend (ESXi).

    Default ``streamOptimized`` is the single-file, self-contained transport
    subformat; the ESXi driver inflates it to a bootable, growable VMFS disk on
    the backend side (ESXI-3 / ESXI-S2). The subformat is selectable because the
    runnable-disk inflate path constrains which transport vmdk it accepts.
    """
    return convert(src, dst, out_format="vmdk", in_format="qcow2", subformat=subformat)


def vmdk_to_qcow2(src: Path, dst: Path) -> Path:
    """vmdk → qcow2 to read a built/exported backend disk back into the cache.

    The source is a single self-contained vmdk the backend exported (no backing
    chain — the ABC ``download_from_pool`` invariant, base.py), so a plain
    convert reproduces the canonical qcow2.
    """
    return convert(src, dst, out_format="qcow2", in_format="vmdk")


__all__ = [
    "convert",
    "qcow2_to_vmdk",
    "qemu_img_path",
    "require_qemu_img",
    "vmdk_to_qcow2",
]
