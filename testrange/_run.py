"""Ephemeral per-run scratch space.

Each :class:`~testrange.backends.libvirt.Orchestrator` entry creates a fresh
:class:`RunDir` — a throwaway directory that holds install-phase
overlays, per-run boot overlays, and cloud-init seed ISOs.  On
orchestrator exit the directory is removed regardless of whether the
test passed, failed, or crashed.

Run state is **not** part of the persistent cache — that's reserved for
base images and post-install snapshots.  See :mod:`testrange.cache`.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from testrange import _qemu_img
from testrange.exceptions import CacheError

_DEFAULT_PREFIX = "testrange-run-"
"""Prefix for run directories created under the system tempdir."""


class RunDir:
    """A short-lived directory holding one test run's scratch artefacts.

    The directory is created eagerly in ``__init__`` with mode ``0755``
    so the ``qemu:///system`` daemon can read disk images placed
    inside it.  Call :meth:`cleanup` to remove it.

    :param root: Optional parent directory.  Defaults to the system
        tempdir; tests can pin this to a ``tmp_path``.
    """

    run_id: str
    """UUID4 string identifying this run."""

    path: Path
    """Absolute path to the run's scratch directory."""

    def __init__(self, root: Path | None = None) -> None:
        self.run_id = str(uuid.uuid4())
        try:
            self.path = Path(
                tempfile.mkdtemp(
                    prefix=f"{_DEFAULT_PREFIX}{self.run_id[:8]}-",
                    dir=str(root) if root is not None else None,
                )
            )
            # mkdtemp defaults to 0o700; widen to 0o755 for the daemon.
            self.path.chmod(0o755)
        except OSError as exc:
            raise CacheError(f"Cannot create run directory: {exc}") from exc

    def create_overlay(self, vm_name: str, backing_path: Path) -> Path:
        """Create a qcow2 overlay for *vm_name* backed by *backing_path*.

        :param vm_name: VM name; becomes the file stem.
        :param backing_path: Absolute path to the backing image.
        :returns: Path to the new overlay (``<vm_name>.qcow2``).
        :raises CacheError: If ``qemu-img create`` fails.
        """
        overlay = self.path / f"{vm_name}.qcow2"
        _qemu_img.create_overlay(backing_path, overlay)
        return overlay

    def create_install_disk(
        self, vm_name: str, base_path: Path, disk_size: str
    ) -> Path:
        """Create a working disk for the install phase.

        Creates a qcow2 overlay on *base_path* and resizes it to
        *disk_size* so cloud-init's ``growpart`` expands the root
        partition on first boot.

        :param vm_name: VM name; becomes the file stem.
        :param base_path: Downloaded base cloud image.
        :param disk_size: ``qemu-img``-compatible size (e.g. ``'64G'``).
        :returns: Path to the resized working disk
            (``<vm_name>-install.qcow2``).
        :raises CacheError: If ``qemu-img`` fails.
        """
        work_disk = self.path / f"{vm_name}-install.qcow2"
        _qemu_img.create_overlay(base_path, work_disk)
        _qemu_img.resize(work_disk, disk_size)
        return work_disk

    def create_blank_disk(self, vm_name: str, disk_size: str) -> Path:
        """Create a blank qcow2 for the Windows install phase.

        Unlike :meth:`create_install_disk`, this does not overlay an
        existing base image — Windows installs from an ISO onto an
        empty disk.

        :param vm_name: VM name; becomes the file stem.
        :param disk_size: ``qemu-img``-compatible size (e.g. ``'40G'``).
        :returns: Path to the new blank qcow2
            (``<vm_name>-install.qcow2``).
        :raises CacheError: If ``qemu-img create`` fails.
        """
        work_disk = self.path / f"{vm_name}-install.qcow2"
        _qemu_img.create_blank(work_disk, disk_size)
        return work_disk

    def unattend_iso_path(self, vm_name: str) -> Path:
        """Return the destination path for a Windows autounattend seed ISO.

        :param vm_name: VM name; becomes the file stem.
        :returns: ``<vm_name>-unattend.iso``.
        """
        return self.path / f"{vm_name}-unattend.iso"

    def nvram_path(self, vm_name: str) -> Path:
        """Return the destination path for a UEFI VM's per-run NVRAM.

        libvirt copies OVMF_VARS onto this path when the domain first
        starts; cleanup happens with the rest of the run dir.

        :param vm_name: VM name; becomes the file stem.
        :returns: ``<vm_name>_VARS.fd``.
        """
        return self.path / f"{vm_name}_VARS.fd"

    def seed_iso_path(self, vm_name: str, *, install: bool) -> Path:
        """Return the destination path for a cloud-init seed ISO.

        :param vm_name: VM name; becomes the file stem.
        :param install: ``True`` for the phase-1 (install) seed,
            ``False`` for the phase-2 (run) seed.
        :returns: ``<vm_name>-install-seed.iso`` or ``<vm_name>-seed.iso``.
        """
        suffix = "-install-seed.iso" if install else "-seed.iso"
        return self.path / f"{vm_name}{suffix}"

    def cleanup(self) -> None:
        """Remove the run directory.  Safe to call multiple times."""
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
