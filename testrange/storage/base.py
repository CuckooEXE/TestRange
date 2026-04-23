"""Abstract storage backend.

The backend is the boundary between "outer Python does orchestration
logic" and "some host reads actual disk bytes for libvirtd / the
hypervisor control plane."  When the outer host and the libvirtd host
are the same machine (the :class:`LocalStorageBackend` case) the
methods collapse to direct filesystem + subprocess ops.  When they
differ (remote libvirt, nested orchestration, Proxmox with uploads),
the same signatures route the work over SFTP / SSH exec / REST
uploads.

Contract
--------

All ``ref`` arguments and return values are **backend-local strings**
— typically an absolute filesystem path on the backend's host.
Callers are expected to treat them as opaque identifiers and never
``Path()``-parse them: a future Proxmox backend will return volume IDs
like ``"local-lvm:vm-100-disk-0"`` instead.

The backend owns three subtrees under its :attr:`cache_root`:

- ``images/`` — source qcow2/img/iso files staged from the outer
  host.  Keyed by content hash; reused across runs.
- ``vms/`` — post-install VM snapshots (``<config_hash>.qcow2``) and
  their manifests (``<config_hash>.json``).  Also keyed for reuse.
- ``runs/<run_id>/`` — per-run scratch: install-phase work disks,
  run-phase overlays, seed ISOs, NVRAM files.  Deleted on
  :meth:`cleanup_run`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class AbstractStorageBackend(ABC):
    """Primitives the orchestrator needs to put disk bytes where the
    hypervisor can read them, and to run ``qemu-img`` against them.

    Every method takes and returns **backend-local** strings — paths
    valid on the host where libvirtd (or the equivalent) is running.
    """

    # ------------------------------------------------------------------
    # Cache root + per-run scratch
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def cache_root(self) -> str:
        """Backend-local path of the persistent cache root.

        Contains ``images/`` (staged sources) and ``vms/`` (install
        snapshots + manifests).  Durable across runs.
        """

    @abstractmethod
    def make_run_dir(self, run_id: str) -> str:
        """Create and return the backend-local path for this run's
        ephemeral scratch directory.  Safe to call more than once for
        the same *run_id*.
        """

    @abstractmethod
    def cleanup_run(self, run_id: str) -> None:
        """Remove the run directory created by :meth:`make_run_dir`.

        Never raises; log-and-swallow semantics are appropriate here
        (teardown must not mask the original exception).
        """

    # ------------------------------------------------------------------
    # File primitives — straight path ops.
    # ------------------------------------------------------------------

    @abstractmethod
    def exists(self, ref: str) -> bool:
        """True if *ref* exists on the backend."""

    @abstractmethod
    def size(self, ref: str) -> int:
        """Size of *ref* in bytes.  Raises :class:`FileNotFoundError`
        (or backend-equivalent) when missing."""

    @abstractmethod
    def write_bytes(self, ref: str, data: bytes, mode: int = 0o644) -> None:
        """Create or overwrite *ref* with *data*, setting permissions to
        *mode*.  Parent directories are created as needed.

        Intended for small artefacts: cloud-init seed ISOs, unattend
        ISOs, manifest JSON.  Use :meth:`upload` for multi-GB disks.
        """

    @abstractmethod
    def read_bytes(self, ref: str) -> bytes:
        """Read and return all bytes of *ref*."""

    @abstractmethod
    def remove(self, ref: str) -> None:
        """Best-effort remove of a single file.  No-op if missing."""

    @abstractmethod
    def makedirs(self, ref: str, mode: int = 0o755) -> None:
        """``mkdir -p`` equivalent with an explicit mode on the final
        component."""

    # ------------------------------------------------------------------
    # Bulk transfer — outer host ↔ backend.
    # ------------------------------------------------------------------

    @abstractmethod
    def upload(self, local_path: Path, ref: str) -> None:
        """Copy *local_path* (outer host) to *ref* (backend).

        Parent directories created as needed.  Optimized for large
        files — implementations should stream rather than buffer the
        whole file in memory.
        """

    @abstractmethod
    def download(self, ref: str, local_path: Path) -> None:
        """Copy *ref* (backend) to *local_path* (outer host)."""

    # ------------------------------------------------------------------
    # ``qemu-img`` primitives — these are the only subprocess work we do.
    # Each backend runs them on whichever side owns the bytes.
    # ------------------------------------------------------------------

    @abstractmethod
    def qemu_img_create_overlay(
        self, backing_ref: str, dest_ref: str
    ) -> None:
        """``qemu-img create -f qcow2 -b <backing> -F qcow2 <dest>``."""

    @abstractmethod
    def qemu_img_create_blank(self, dest_ref: str, size: str) -> None:
        """``qemu-img create -f qcow2 <dest> <size>`` — no backing."""

    @abstractmethod
    def qemu_img_resize(self, ref: str, size: str) -> None:
        """``qemu-img resize <ref> <size>``."""

    @abstractmethod
    def qemu_img_convert_compressed(
        self, src_ref: str, dest_ref: str
    ) -> None:
        """``qemu-img convert -f qcow2 -O qcow2 -c <src> <dest>``."""

    # ------------------------------------------------------------------
    # Convenience path helpers (don't touch disk; override is optional).
    # ------------------------------------------------------------------

    def images_dir(self) -> str:
        """Return ``<cache_root>/images`` as a backend-local string."""
        return self._join(self.cache_root, "images")

    def vms_dir(self) -> str:
        """Return ``<cache_root>/vms`` as a backend-local string."""
        return self._join(self.cache_root, "vms")

    def run_dir(self, run_id: str) -> str:
        """Return ``<cache_root>/runs/<run_id>`` as a backend-local string.

        Pure computation — does not create the directory.  Use
        :meth:`make_run_dir` to create.
        """
        return self._join(self.cache_root, "runs", run_id)

    def _join(self, *parts: str) -> str:
        """Join backend-local path components with forward slashes.

        Backends that represent refs as POSIX paths can share this
        default; backends that use a different path convention (URIs,
        Proxmox volume IDs) must override.
        """
        return "/".join(p.rstrip("/") for p in parts)
