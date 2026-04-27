"""Abstract file + exec transport.

This is the "where does the filesystem live" half of a storage
backend.  It knows nothing about disk formats — it only knows how
to put bytes at refs, read them back, and run a subprocess against
wherever its refs point.

Pair with an :class:`~testrange.storage.disk.AbstractDiskFormat` to
form a full :class:`~testrange.storage.StorageBackend`.

Contract
--------

All ``ref`` arguments and return values are **transport-local strings**
— for :class:`LocalFileTransport` that's an absolute POSIX path on
the outer host; for :class:`SSHFileTransport` it's a path on the
remote.  A REST-style transport would return volume IDs that don't
parse as paths at all.  Callers treat refs as opaque.

The transport owns three subtrees under :attr:`cache_root`:

- ``images/`` — source disk-image / ISO files staged from the outer host.
- ``vms/``    — post-install snapshots + manifests.
- ``runs/<run_id>/`` — per-run scratch (overlays, seed ISOs, NVRAM).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class AbstractFileTransport(ABC):
    """Transport-local file + exec primitives.

    Every method takes and returns **transport-local** strings — refs
    valid on the host this transport reaches.  Nothing in here knows
    about any specific disk format; that lives on
    :class:`~testrange.storage.disk.AbstractDiskFormat`.
    """

    # ------------------------------------------------------------------
    # Cache root + per-run scratch
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def cache_root(self) -> str:
        """Transport-local path of the persistent cache root."""

    @abstractmethod
    def make_run_dir(self, run_id: str) -> str:
        """Create and return the transport-local path for this run's
        ephemeral scratch directory.  Idempotent."""

    @abstractmethod
    def cleanup_run(self, run_id: str) -> None:
        """Remove the run directory.  Never raises — teardown must not
        mask the original exception."""

    # ------------------------------------------------------------------
    # File primitives — straight path ops.
    # ------------------------------------------------------------------

    @abstractmethod
    def exists(self, ref: str) -> bool:
        """True if *ref* exists."""

    @abstractmethod
    def size(self, ref: str) -> int:
        """Size of *ref* in bytes."""

    @abstractmethod
    def write_bytes(self, ref: str, data: bytes, mode: int = 0o644) -> None:
        """Create or overwrite *ref* with *data*, permissions *mode*.
        Parent directories are created as needed.

        Intended for small artefacts (seed ISOs, manifest JSON).  Use
        :meth:`upload` for large files."""

    @abstractmethod
    def read_bytes(self, ref: str) -> bytes:
        """Read and return all bytes of *ref*."""

    @abstractmethod
    def remove(self, ref: str) -> None:
        """Best-effort remove.  No-op when missing."""

    @abstractmethod
    def rename(self, src_ref: str, dst_ref: str) -> None:
        """Atomically rename *src_ref* to *dst_ref*.

        If *dst_ref* already exists it is replaced.  Both refs must
        live on the same filesystem so the operation is a true atomic
        rename — used by :meth:`CacheManager.store_vm` to publish a
        freshly-compressed cache entry only once the compress step
        has finished writing.
        """

    @abstractmethod
    def makedirs(self, ref: str, mode: int = 0o755) -> None:
        """``mkdir -p`` with an explicit mode on the final component."""

    # ------------------------------------------------------------------
    # Bulk transfer — outer host ↔ transport host.
    # ------------------------------------------------------------------

    @abstractmethod
    def upload(self, local_path: Path, ref: str) -> None:
        """Copy *local_path* (outer host) to *ref* (transport host).
        Parent directories created.  Streams — no in-memory buffering
        of the full file."""

    @abstractmethod
    def download(self, ref: str, local_path: Path) -> None:
        """Copy *ref* (transport host) to *local_path* (outer host)."""

    # ------------------------------------------------------------------
    # Generic tool execution — the exec primitive disk-format layers
    # build on.
    # ------------------------------------------------------------------

    @abstractmethod
    def run_tool(
        self,
        argv: list[str],
        timeout: float = 60.0,
    ) -> tuple[int, bytes, bytes]:
        """Execute *argv* on the transport's host.

        Returns ``(exit_code, stdout, stderr)``.  Used by
        :class:`~testrange.storage.disk.AbstractDiskFormat`
        implementations to run disk-manipulation tools wherever the
        filesystem lives.  The transport itself never interprets
        the argv — it's just a pipe to a subprocess on the right
        host.

        :raises testrange.exceptions.CacheError: On exec-infrastructure
            failure (SSH disconnect, etc.).  Non-zero exit codes are
            returned in the tuple; the caller decides whether to
            treat them as errors.
        """

    # ------------------------------------------------------------------
    # Convenience path helpers (don't touch disk; override is optional).
    # ------------------------------------------------------------------

    def images_dir(self) -> str:
        """Return ``<cache_root>/images``."""
        return self._join(self.cache_root, "images")

    def vms_dir(self) -> str:
        """Return ``<cache_root>/vms``."""
        return self._join(self.cache_root, "vms")

    def run_dir(self, run_id: str) -> str:
        """Return ``<cache_root>/runs/<run_id>`` (pure computation;
        use :meth:`make_run_dir` to actually create it)."""
        return self._join(self.cache_root, "runs", run_id)

    def _join(self, *parts: str) -> str:
        """POSIX-style path join.  Transports that don't use
        path-shaped refs (future Proxmox volume IDs) override this."""
        return "/".join(p.rstrip("/") for p in parts)
