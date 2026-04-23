"""Local storage backend — filesystem + subprocess on the outer host.

Identity wrapper: every method is the direct filesystem or subprocess
call the orchestrator used to make inline when it assumed the
hypervisor ran on the same machine as Python.  Preserves today's
behaviour bit-for-bit; exists so the rest of the codebase can talk
to storage through :class:`AbstractStorageBackend` without branching
on backend type.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from testrange._qemu_img import (
    convert_compressed as _qemu_img_convert_compressed,
)
from testrange._qemu_img import (
    create_blank as _qemu_img_create_blank,
)
from testrange._qemu_img import (
    create_overlay as _qemu_img_create_overlay,
)
from testrange._qemu_img import (
    resize as _qemu_img_resize,
)
from testrange.exceptions import CacheError
from testrange.storage.base import AbstractStorageBackend


class LocalStorageBackend(AbstractStorageBackend):
    """Filesystem-and-subprocess backend for the outer host.

    :param cache_root: Backend cache root.  Defaults to the value
        :class:`~testrange.cache.CacheManager` resolves — typically
        ``/var/tmp/testrange/<user>`` or ``$TESTRANGE_CACHE_DIR``.
    """

    _cache_root: Path

    def __init__(self, cache_root: Path) -> None:
        self._cache_root = cache_root.expanduser().resolve()

    @property
    def cache_root(self) -> str:
        return str(self._cache_root)

    # ------------------------------------------------------------------
    # Per-run scratch
    # ------------------------------------------------------------------

    def make_run_dir(self, run_id: str) -> str:
        run_path = Path(self.run_dir(run_id))
        try:
            run_path.mkdir(parents=True, exist_ok=True)
            # 0755 so the hypervisor process (which typically runs as a
            # dedicated system user, not the orchestrator's user) can
            # read disk images placed inside.
            run_path.chmod(0o755)
        except OSError as exc:
            raise CacheError(
                f"Cannot create run directory {run_path}: {exc}"
            ) from exc
        return str(run_path)

    def cleanup_run(self, run_id: str) -> None:
        run_path = Path(self.run_dir(run_id))
        if run_path.exists():
            shutil.rmtree(run_path, ignore_errors=True)

    # ------------------------------------------------------------------
    # File primitives
    # ------------------------------------------------------------------

    def exists(self, ref: str) -> bool:
        return Path(ref).exists()

    def size(self, ref: str) -> int:
        return Path(ref).stat().st_size

    def write_bytes(self, ref: str, data: bytes, mode: int = 0o644) -> None:
        path = Path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)

    def read_bytes(self, ref: str) -> bytes:
        return Path(ref).read_bytes()

    def remove(self, ref: str) -> None:
        path = Path(ref)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                # Best-effort; teardown must never raise.
                pass

    def makedirs(self, ref: str, mode: int = 0o755) -> None:
        path = Path(ref)
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(mode)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Bulk transfer — local-to-local is just copy.  Kept distinct from
    # ``write_bytes`` so we can keep large files out of Python memory.
    # ------------------------------------------------------------------

    def upload(self, local_path: Path, ref: str) -> None:
        dest = Path(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    def download(self, ref: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref, local_path)

    # ------------------------------------------------------------------
    # qemu-img — delegate to the existing typed wrapper.
    # ------------------------------------------------------------------

    def qemu_img_create_overlay(
        self, backing_ref: str, dest_ref: str
    ) -> None:
        _qemu_img_create_overlay(Path(backing_ref), Path(dest_ref))

    def qemu_img_create_blank(self, dest_ref: str, size: str) -> None:
        _qemu_img_create_blank(Path(dest_ref), size)

    def qemu_img_resize(self, ref: str, size: str) -> None:
        _qemu_img_resize(Path(ref), size)

    def qemu_img_convert_compressed(
        self, src_ref: str, dest_ref: str
    ) -> None:
        _qemu_img_convert_compressed(Path(src_ref), Path(dest_ref))
