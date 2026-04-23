"""Ephemeral per-run scratch space.

Each orchestrator entry creates a fresh :class:`RunDir` — a throwaway
directory that holds install-phase overlays, per-run boot overlays,
seed ISOs, and UEFI NVRAM files.  On orchestrator exit the directory
is removed regardless of whether the test passed, failed, or crashed.

Every path produced here lives on the :class:`StorageBackend` the
orchestrator was built with — typically the outer host for a local
backend, or a remote host for an SSH / REST-based one.  Callers should
treat the returned strings as opaque backend-local refs: they are
valid inputs to whatever the backend's hypervisor consumes (domain
XML, API volume IDs, …) but not safe to ``Path()``-parse from outer
Python when the backend is remote.

Run state is **not** part of the persistent cache — that's reserved
for base images and post-install snapshots.  See
:mod:`testrange.cache`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange.storage.base import StorageBackend


class RunDir:
    """A short-lived directory holding one test run's scratch artefacts.

    The directory is created eagerly in ``__init__`` via the backend's
    :meth:`~AbstractStorageBackend.make_run_dir`; call :meth:`cleanup`
    to remove it.

    :param storage: Storage backend where the run dir should live.
        Every path this object returns is backend-local (valid on the
        backend's hypervisor host, not necessarily the outer host).
    """

    run_id: str
    """UUID4 string identifying this run."""

    path: str
    """Backend-local path to the run's scratch directory."""

    _storage: StorageBackend
    """Backend that owns the scratch directory."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self.run_id = str(uuid.uuid4())
        self.path = storage.transport.make_run_dir(self.run_id)

    @property
    def storage(self) -> StorageBackend:
        """Return the backend this run dir is backed by."""
        return self._storage

    def create_overlay(self, vm_name: str, backing_ref: str) -> str:
        """Create a qcow2 overlay for *vm_name* backed by *backing_ref*.

        :param vm_name: VM name; becomes the file stem.
        :param backing_ref: Backend-local ref to the backing image
            (must already exist on the backend).
        :returns: Backend-local ref to the new overlay
            (``<run>/<vm_name>.qcow2``).
        :raises CacheError: If the backend's overlay-create step fails.
        """
        overlay = self._join(f"{vm_name}.qcow2")
        self._storage.disk.create_overlay(backing_ref, overlay)
        return overlay

    def create_install_disk(
        self, vm_name: str, base_ref: str, disk_size: str
    ) -> str:
        """Create a working disk for the install phase.

        Creates a qcow2 overlay on *base_ref* and resizes it to
        *disk_size* so cloud-init's ``growpart`` expands the root
        partition on first boot.

        :param vm_name: VM name; becomes the file stem.
        :param base_ref: Backend-local ref to the base cloud image.
        :param disk_size: qcow2-compatible size string (e.g. ``'64G'``).
        :returns: Backend-local ref to the resized working disk
            (``<run>/<vm_name>-install.qcow2``).
        :raises CacheError: If the backend's image-manipulation step fails.
        """
        work_ref = self._join(f"{vm_name}-install.qcow2")
        self._storage.disk.create_overlay(base_ref, work_ref)
        self._storage.disk.resize(work_ref, disk_size)
        return work_ref

    def create_blank_disk(self, vm_name: str, disk_size: str) -> str:
        """Create a blank qcow2 for the Windows install phase.

        Unlike :meth:`create_install_disk`, this does not overlay an
        existing base image — Windows installs from an ISO onto an
        empty disk.

        :param vm_name: VM name; becomes the file stem.
        :param disk_size: qcow2-compatible size string (e.g. ``'40G'``).
        :returns: Backend-local ref to the new blank qcow2
            (``<run>/<vm_name>-install.qcow2``).
        :raises CacheError: If the backend's blank-disk-create step fails.
        """
        work_ref = self._join(f"{vm_name}-install.qcow2")
        self._storage.disk.create_blank(work_ref, disk_size)
        return work_ref

    def unattend_iso_path(self, vm_name: str) -> str:
        """Return the backend-local ref for a Windows autounattend seed ISO.

        :param vm_name: VM name; becomes the file stem.
        :returns: ``<run>/<vm_name>-unattend.iso``.
        """
        return self._join(f"{vm_name}-unattend.iso")

    def nvram_path(self, vm_name: str) -> str:
        """Return the backend-local ref for a UEFI VM's per-run NVRAM.

        Backends that support UEFI copy the OVMF_VARS template onto
        this path when the domain first starts; cleanup happens with
        the rest of the run dir.

        :param vm_name: VM name; becomes the file stem.
        :returns: ``<run>/<vm_name>_VARS.fd``.
        """
        return self._join(f"{vm_name}_VARS.fd")

    def seed_iso_path(self, vm_name: str, *, install: bool) -> str:
        """Return the backend-local ref for a cloud-init seed ISO.

        :param vm_name: VM name; becomes the file stem.
        :param install: ``True`` for the phase-1 (install) seed,
            ``False`` for the phase-2 (run) seed.
        :returns: ``<run>/<vm_name>-install-seed.iso`` or
            ``<run>/<vm_name>-seed.iso``.
        """
        suffix = "-install-seed.iso" if install else "-seed.iso"
        return self._join(f"{vm_name}{suffix}")

    def cleanup(self) -> None:
        """Remove the run directory.  Safe to call multiple times."""
        self._storage.transport.cleanup_run(self.run_id)

    def _join(self, name: str) -> str:
        """Return a backend-local ref under this run dir."""
        return self.path.rstrip("/") + "/" + name
