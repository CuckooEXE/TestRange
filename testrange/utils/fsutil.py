"""Durable filesystem writes — fsync-before-rename so a rename survives power loss."""

from __future__ import annotations

import os
from pathlib import Path


def durable_replace(tmp: Path, dest: Path) -> None:
    """Atomically and durably move *tmp* onto *dest*.

    fsyncs *tmp*'s data, renames it onto *dest*, then fsyncs *dest*'s parent
    directory. The directory fsync is what makes the rename survive power loss
    (not merely a process crash): without it the rename can be reordered ahead of
    the data writeback, leaving a zero-length or partial file at *dest*.
    """
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp.replace(dest)  # Path.replace delegates to os.replace — atomic
    dir_fd = os.open(dest.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
