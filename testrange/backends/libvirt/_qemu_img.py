"""Typed wrapper around the ``qemu-img`` command-line tool.

``qemu-img`` is the canonical public interface for qcow2 image operations
— the qemu project does not publish a Python library for image I/O, and
no third-party PyPI package covers the create/convert/resize surface we
need.  This module centralises the three subprocess invocations we make
so the rest of the backend reads like typed function calls.

All functions raise :class:`~testrange.exceptions.CacheError` on
subprocess failure, with the tool's stderr included in the message.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from testrange.exceptions import CacheError


def _run(argv: list[str]) -> None:
    """Invoke ``qemu-img`` and raise :class:`CacheError` on non-zero exit.

    :param argv: Command argv starting with ``'qemu-img'``.
    :raises CacheError: If the tool exits non-zero.  The exception message
        includes the stderr output, stripped.
    """
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        cmd = " ".join(argv[:2])  # e.g. "qemu-img convert"
        raise CacheError(f"{cmd} failed: {exc.stderr.strip()}") from exc


def info(disk: Path) -> dict[str, Any]:
    """Return the JSON metadata reported by ``qemu-img info`` for *disk*.

    :param disk: Path to the qcow2/img file to inspect.
    :returns: Parsed JSON dict (keys include ``format``, ``virtual-size``,
        ``actual-size``, ``backing-filename`` when applicable).
    :raises CacheError: If ``qemu-img info`` fails or returns malformed JSON.
    """
    argv = ["qemu-img", "info", "--output=json", str(disk)]
    try:
        result = subprocess.run(argv, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise CacheError(
            f"qemu-img info failed: {exc.stderr.strip()}"
        ) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CacheError(
            f"qemu-img info returned non-JSON output: {result.stdout!r}"
        ) from exc


def create_overlay(backing_path: Path, dest: Path) -> None:
    """Create a qcow2 overlay with *backing_path* as its backing file.

    :param backing_path: Existing qcow2 to use as the backing store.
    :param dest: Destination path for the new overlay.
    :raises CacheError: If ``qemu-img create`` fails.
    """
    _run(
        [
            "qemu-img", "create",
            "-f", "qcow2",
            "-b", str(backing_path),
            "-F", "qcow2",
            str(dest),
        ]
    )


def create_blank(dest: Path, size: str) -> None:
    """Create an empty sparse qcow2 of *size* at *dest* (no backing file).

    Used by the Windows install path, which boots a fresh installer
    onto an empty disk rather than overlaying a pre-existing cloud
    image like the Linux path does.

    :param dest: Destination path.
    :param size: ``qemu-img``-compatible size string (e.g. ``'40G'``).
    :raises CacheError: If ``qemu-img create`` fails.
    """
    _run(
        [
            "qemu-img", "create",
            "-f", "qcow2",
            str(dest),
            size,
        ]
    )


def resize(disk: Path, size: str) -> None:
    """Resize a qcow2 image to *size*.

    :param disk: Path to the qcow2 image.
    :param size: Size string accepted by ``qemu-img resize`` (e.g.
        ``'64G'``).  May be absolute or a delta (``'+20G'``).
    :raises CacheError: If ``qemu-img resize`` fails.
    """
    _run(["qemu-img", "resize", str(disk), size])


def convert_compressed(src: Path, dest: Path) -> None:
    """Convert *src* to a compressed qcow2 at *dest*.

    Equivalent to ``qemu-img convert -f qcow2 -O qcow2 -c``.  Used when
    promoting a freshly-installed disk into the persistent cache so the
    archived copy is small.

    :param src: Source qcow2 image.
    :param dest: Destination path for the compressed copy.
    :raises CacheError: If ``qemu-img convert`` fails.
    """
    _run(
        [
            "qemu-img", "convert",
            "-f", "qcow2",
            "-O", "qcow2",
            "-c",
            str(src),
            str(dest),
        ]
    )
