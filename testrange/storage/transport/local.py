"""Local file transport — filesystem + subprocess on the outer host.

Identity wrapper: every method is the direct filesystem or subprocess
call the orchestrator used to make inline when it assumed the
hypervisor ran on the same machine.  Preserves today's behaviour
bit-for-bit; exists so the rest of the codebase can talk through
:class:`AbstractFileTransport` without branching on transport type.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from testrange.exceptions import CacheError
from testrange.storage.transport.base import AbstractFileTransport


class LocalFileTransport(AbstractFileTransport):
    """Outer-host filesystem + local subprocess.

    :param cache_root: Cache root directory.  Defaults to the value
        :class:`~testrange.cache.CacheManager` resolves — typically
        ``/var/tmp/testrange/<user>``.
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
    # Bulk transfer — local-to-local is just a file copy.
    # ------------------------------------------------------------------

    def upload(self, local_path: Path, ref: str) -> None:
        dest = Path(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)

    def download(self, ref: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ref, local_path)

    # ------------------------------------------------------------------
    # Tool execution — subprocess on the outer host.
    # ------------------------------------------------------------------

    def run_tool(
        self,
        argv: list[str],
        timeout: float = 60.0,
    ) -> tuple[int, bytes, bytes]:
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CacheError(
                f"local tool {argv[0]!r} timed out after {timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise CacheError(
                f"local tool {argv[0]!r} is not installed: {exc}"
            ) from exc
        return result.returncode, result.stdout, result.stderr
