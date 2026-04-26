"""Ephemeral per-run scratch space.

Each orchestrator entry creates a fresh :class:`RunDir` — a throwaway
directory that holds install-phase overlays, per-run boot overlays,
seed ISOs, and any other backend-managed scratch files.  On
orchestrator exit the directory is removed regardless of whether the
test passed, failed, or crashed.

Every path produced here lives on the :class:`StorageBackend` the
orchestrator was built with — typically the outer host for a local
backend, or a remote host for an SSH / REST-based one.  Callers should
treat the returned strings as opaque backend-local refs: they are
valid inputs to whatever the backend's hypervisor consumes (domain
configuration, API volume IDs, …) but not safe to ``Path()``-parse
from outer Python when the backend is remote.

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

    def path_for(self, name: str) -> str:
        """Return a backend-local ref ``<run>/<name>``.

        Backends that need scratch files outside the standard
        overlay / install-disk / seed-ISO set (firmware-state blobs,
        per-VM config files, …) compose their own filenames via this
        helper.
        """
        return self._join(name)

    def create_overlay(self, vm_name: str, backing_ref: str) -> str:
        """Create a copy-on-write overlay for *vm_name* backed by *backing_ref*.

        :param vm_name: VM name; becomes the file stem.
        :param backing_ref: Backend-local ref to the backing image
            (must already exist on the backend).
        :returns: Backend-local ref to the new overlay
            (``<run>/<vm_name>{disk_extension}``).
        :raises CacheError: If the backend's overlay-create step fails.
        """
        overlay = self._join(f"{vm_name}{self._storage.disk.disk_extension}")
        self._storage.disk.create_overlay(backing_ref, overlay)
        return overlay

    def create_install_disk(
        self, vm_name: str, base_ref: str, disk_size: str
    ) -> str:
        """Create a working disk for the install phase.

        Creates an overlay on *base_ref* and resizes it to *disk_size*
        so cloud-init's ``growpart`` expands the root partition on
        first boot.

        :param vm_name: VM name; becomes the file stem.
        :param base_ref: Backend-local ref to the base cloud image.
        :param disk_size: Size string in the disk format's accepted
            syntax (e.g. ``'64G'``).
        :returns: Backend-local ref to the resized working disk
            (``<run>/<vm_name>-install{disk_extension}``).
        :raises CacheError: If the backend's image-manipulation step fails.
        """
        ext = self._storage.disk.disk_extension
        work_ref = self._join(f"{vm_name}-install{ext}")
        self._storage.disk.create_overlay(base_ref, work_ref)
        self._storage.disk.resize(work_ref, disk_size)
        return work_ref

    def create_blank_disk(self, vm_name: str, disk_size: str) -> str:
        """Create a blank disk for an install that needs an empty target.

        Unlike :meth:`create_install_disk`, this does not overlay an
        existing base image — used by installers (e.g. Windows) that
        run from an ISO onto an empty disk.

        :param vm_name: VM name; becomes the file stem.
        :param disk_size: Size string in the disk format's accepted
            syntax (e.g. ``'40G'``).
        :returns: Backend-local ref to the new blank disk
            (``<run>/<vm_name>-install{disk_extension}``).
        :raises CacheError: If the backend's blank-disk-create step fails.
        """
        ext = self._storage.disk.disk_extension
        work_ref = self._join(f"{vm_name}-install{ext}")
        self._storage.disk.create_blank(work_ref, disk_size)
        return work_ref

    def cleanup(self) -> None:
        """Remove the run directory.  Safe to call multiple times."""
        self._storage.transport.cleanup_run(self.run_id)

    def _join(self, name: str) -> str:
        """Return a backend-local ref under this run dir."""
        return self.path.rstrip("/") + "/" + name
