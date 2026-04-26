"""Persistent disk-image cache.

The cache lives under ``/var/tmp/testrange/<user>/`` by default and can
be overridden via the ``TESTRANGE_CACHE_DIR`` environment variable.
See the installation docs for the permission requirements on custom
paths.

Layout
------

Two things live in the cache, and nothing else:

1. **Base images** downloaded from upstream (``images/``)
2. **Post-install VM snapshots** built by the orchestrator (``vms/``)

::

    <cache_root>/
    ├── images/
    │   ├── <url_hash><ext>               # extension copied from source URL
    │   └── <url_hash>.meta.json          # URL, size, sha256, timestamp
    └── vms/
        └── <config_hash>/                # one directory per cached VM
            ├── <primary disk>            # filename owned by the disk format
            ├── manifest.json             # build manifest (what installed)
            └── ...                       # backend-specific resources

Each cached VM lives in its own directory under ``vms/`` so all of
its **resources** sit together.  Two are universal across backends
— the primary disk and the ``manifest.json`` describing how it was
built — and any backend may drop additional files alongside them
(extra drives, hypervisor-specific config blobs, firmware-state
snapshots, …) without the cache layer needing to know about them.
The primary disk's filename is owned by the backend's disk format
(see :attr:`~testrange.storage.disk.AbstractDiskFormat.primary_disk_filename`),
so a backend that uses a non-qcow2 format produces the right
extension automatically.  The ``manifest.json`` records the exact
set of modifications applied to the base image; inspect it with
any JSON viewer to audit a cached build without booting it.

Ephemeral per-run scratch space (install-phase overlays, seed ISOs) is
**not** part of the cache — see :class:`~testrange._run.RunDir`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from filelock import FileLock
from tqdm import tqdm

from testrange._logging import get_logger, log_duration
from testrange.exceptions import CacheError, ImageNotFoundError

if TYPE_CHECKING:
    from testrange.cache_http import HttpCache
    from testrange.storage.base import StorageBackend

_log = get_logger(__name__)

_DEFAULT_CACHE_ROOT = Path(
    os.environ.get(
        "TESTRANGE_CACHE_DIR",
        str(
            Path("/var/tmp/testrange")
            / (os.environ.get("USER") or str(os.getuid()))
        ),
    )
)
"""Default root directory for the TestRange cache.

Reads ``TESTRANGE_CACHE_DIR`` from the environment; falls back to
``/var/tmp/testrange/<user>``.
"""

_DEFAULT_VIRTIO_WIN_URL = (
    "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
    "latest-virtio/virtio-win.iso"
)
"""Upstream location of the signed virtio-win ISO.

Overridable at call time via
:meth:`CacheManager.get_virtio_win_iso`.  The Fedora-hosted ``latest-virtio``
redirect always returns the most recent stable release."""


def _copy_file(src: Path, dest: Path) -> None:
    """Copy *src* to *dest* with shutil, isolated for easy mocking in tests."""
    shutil.copyfile(src, dest)


def _url_extension(url: str) -> str:
    """Return *url*'s filename extension, including the leading dot.

    Falls back to ``""`` when the URL has no recognisable extension.
    Used by :meth:`CacheManager.get_image` to keep the cache
    format-agnostic — whatever the user pointed at, that's what we
    stash on disk.
    """
    name = url.rsplit("/", 1)[-1].rsplit("?", 1)[0]
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


class CacheManager:
    """Manages the TestRange disk-image cache.

    Only two kinds of artefact live here: downloaded base OS images, and
    compressed post-install VM snapshots.  Ephemeral per-run scratch
    files belong to :class:`~testrange._run.RunDir` instead.

    :param root: Base directory for all cached data.  Defaults to
        ``/var/tmp/testrange/<user>`` (overridable via ``TESTRANGE_CACHE_DIR``).
    :param remote: Optional :class:`~testrange.cache_http.HttpCache`
        consulted as a second-tier fill source.  When set, base-image
        downloads and VM-snapshot lookups check the remote on local
        miss; successful local stores are mirrored back to the remote.
        All remote operations are best-effort — failures fall through
        to the cold path.
    """

    root: Path
    """Root directory of the cache (e.g. ``/var/tmp/testrange/alice``)."""

    images_dir: Path
    """Subdirectory holding downloaded base OS images (``<root>/images``)."""

    vms_dir: Path
    """Subdirectory holding per-VM resource directories (``<root>/vms``)."""

    backend_name: str
    """Top-level prefix added to remote-cache VM-resource URL keys so
    one HTTP cache can serve multiple hypervisor backends without
    artifact-format collisions.  Set by the orchestrator that owns
    this CacheManager (the user never specifies it); defaults to
    ``"local"`` for ad-hoc / test-direct construction."""

    remote: HttpCache | None
    """Optional second-tier remote cache; ``None`` disables it."""

    def __init__(
        self,
        root: Path = _DEFAULT_CACHE_ROOT,
        remote: HttpCache | None = None,
    ) -> None:
        self.root = root
        self.images_dir = root / "images"
        self.vms_dir = root / "vms"
        self.backend_name = "local"
        self.remote = remote
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Create the cache directory tree if it does not exist.

        :raises CacheError: If directory creation fails due to permissions.
        """
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.vms_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o755)
        except OSError as exc:
            raise CacheError(f"Cannot create cache directory: {exc}") from exc

    def get_image(self, url: str) -> Path:
        """Return the local path for a base image, downloading if necessary.

        Downloads are streamed with a progress bar.  A ``.meta.json``
        sidecar records the source URL, download timestamp, and SHA-256
        of the file content for integrity verification on subsequent
        cache hits.

        :param url: An ``https://`` URL pointing to a base image
            (cloud disk, installer ISO, or any other artifact a
            backend can build a VM from).  The cached file's
            extension is taken verbatim from the URL so the cache
            stays format-agnostic.
        :returns: Path to the locally cached image file.
        :raises ImageNotFoundError: If the download fails (HTTP error,
            network timeout, etc.).
        :raises CacheError: If the file cannot be written to the cache.
        """
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
        meta_path = self.images_dir / f"{url_hash}.meta.json"
        ext = _url_extension(url)
        image_path = self.images_dir / f"{url_hash}{ext}"

        lock_path = self.images_dir / f"{url_hash}.lock"
        remote_image_key = f"images/{url_hash}{ext}"
        remote_meta_key = f"images/{url_hash}.meta.json"
        with FileLock(str(lock_path), timeout=1800):
            if image_path.exists() and meta_path.exists():
                _log.debug("base image cache hit for %s", url)
                return image_path

            # Second-tier: HTTP cache (best-effort).  A hit avoids a
            # round-trip to the upstream mirror, which for a 600 MiB
            # Debian image is the difference between a few seconds and
            # a few minutes on a slow link.
            if self._fill_image_from_remote(
                url, remote_image_key, remote_meta_key, image_path, meta_path,
            ):
                return image_path

            _log.info("downloading base image from %s", url)
            try:
                with log_duration(_log, f"download {image_path.name}"):
                    self._download(url, image_path)
            except requests.RequestException as exc:
                raise ImageNotFoundError(f"Failed to download {url!r}: {exc}") from exc

            file_sha256 = _sha256_file(image_path)
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "downloaded_at": time.time(),
                        "sha256": file_sha256,
                        "size_bytes": image_path.stat().st_size,
                    },
                    indent=2,
                )
            )
            self._publish_image_to_remote(
                remote_image_key, remote_meta_key, image_path, meta_path,
            )
        return image_path

    def _fill_image_from_remote(
        self,
        url: str,
        image_key: str,
        meta_key: str,
        image_path: Path,
        meta_path: Path,
    ) -> bool:
        """Try to populate *image_path* + *meta_path* from the HTTP
        cache.  Returns ``True`` on hit, ``False`` on miss / disabled."""
        if self.remote is None:
            return False
        if not self.remote.exists(image_key):
            return False
        with log_duration(_log, f"http-cache fill {image_path.name}"):
            if not self.remote.get(image_key, image_path):
                return False
        if not self.remote.get(meta_key, meta_path):
            # Image came down but meta sidecar is missing; synthesise a
            # minimal one so get_image's hit-check on the next run
            # passes without re-downloading.
            file_sha256 = _sha256_file(image_path)
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "downloaded_at": time.time(),
                        "sha256": file_sha256,
                        "size_bytes": image_path.stat().st_size,
                        "source": "http-cache (missing meta.json)",
                    },
                    indent=2,
                )
            )
        _log.info("base image filled from http-cache for %s", url)
        return True

    def _publish_image_to_remote(
        self,
        image_key: str,
        meta_key: str,
        image_path: Path,
        meta_path: Path,
    ) -> None:
        """Best-effort PUT of a freshly-downloaded image + meta to the
        HTTP cache.  Failures are logged at WARN by HttpCache and do
        not raise."""
        if self.remote is None:
            return
        with log_duration(_log, f"http-cache publish {image_path.name}"):
            self.remote.put(image_key, image_path)
        self.remote.put(meta_key, meta_path)

    @staticmethod
    def _download(url: str, dest: Path) -> None:
        """Stream *url* to *dest* with a tqdm progress bar.

        :param url: Remote URL to download.
        :param dest: Local destination path.
        :raises requests.RequestException: On HTTP or network errors.
        """
        tmp = dest.with_suffix(".tmp")
        with requests.get(url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with (
                open(tmp, "wb") as fh,
                tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    desc=dest.name,
                ) as bar,
            ):
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    bar.update(len(chunk))
        tmp.rename(dest)

    def stage_local_iso(self, src: Path) -> Path:
        """Copy *src* into the cache so it lives at a stable, managed path.

        Content-hashes the ISO and copies it into the cache on first
        use; subsequent calls return the same cache path without
        re-copying.  Files that are already under the cache root are
        returned as-is.

        :param src: Absolute path to a local ISO the caller wants to
            attach as a CD-ROM.
        :returns: Path to an equivalent copy under ``<cache_root>/images``.
        :raises ImageNotFoundError: If *src* does not exist.
        :raises CacheError: On filesystem errors.
        """
        src = src.expanduser().resolve()
        if not src.exists():
            raise ImageNotFoundError(f"ISO {src} does not exist")

        try:
            src.relative_to(self.root)
            return src
        except ValueError:
            pass

        sha = _sha256_file(src)[:24]
        dest = self.images_dir / f"iso-{sha}.iso"
        lock_path = self.images_dir / f"iso-{sha}.lock"
        with FileLock(str(lock_path), timeout=1800):
            if dest.exists():
                _log.debug("local ISO cache hit (iso-%s)", sha)
                return dest
            _log.info("staging local ISO %s → %s", src, dest)
            tmp = dest.with_suffix(".iso.part")
            with log_duration(_log, f"copy ISO {src.name!r} into cache"):
                _copy_file(src, tmp)
            os.chmod(tmp, 0o644)
            tmp.rename(dest)
        return dest

    def get_virtio_win_iso(
        self,
        url: str = _DEFAULT_VIRTIO_WIN_URL,
    ) -> Path:
        """Return the local path to the virtio-win ISO, downloading once.

        Windows Setup can't see the virtio-blk / virtio-net / SCSI
        devices QEMU exposes without these drivers.  The ISO also
        bundles the ``qemu-guest-agent`` MSI installer, which our
        autounattend ``FirstLogonCommands`` uses to bootstrap the
        guest-agent channel.

        The ISO is cached under ``<cache_root>/images/`` with a
        stable ``virtio-win.iso`` filename (no URL hash) so that
        downstream domain XML can reference it by a predictable path.

        :param url: HTTPS URL to download if the ISO is not yet cached.
            Defaults to the ``latest-virtio`` RPM repo mirror.
        :returns: Path to the cached ``virtio-win.iso``.
        :raises ImageNotFoundError: On download failure.
        :raises CacheError: On filesystem errors.
        """
        dest = self.images_dir / "virtio-win.iso"
        lock_path = self.images_dir / "virtio-win.iso.lock"
        with FileLock(str(lock_path), timeout=1800):
            if dest.exists():
                _log.debug("virtio-win.iso cache hit")
                return dest
            _log.info("downloading virtio-win.iso from %s", url)
            try:
                with log_duration(_log, "download virtio-win.iso"):
                    self._download(url, dest)
            except requests.RequestException as exc:
                raise ImageNotFoundError(
                    f"Failed to download virtio-win ISO from {url!r}: {exc}"
                ) from exc
            os.chmod(dest, 0o644)
        return dest

    # ------------------------------------------------------------------
    # Backend-aware staging + snapshot cache.
    #
    # All methods below take a StorageBackend (the orchestrator's) and
    # route disk work to wherever the hypervisor's control plane
    # actually reads from.  For the LocalStorageBackend case every op
    # collapses to the same local filesystem action the pre-backend
    # code did inline; for remote backends the bytes flow over the
    # backend's transport (SFTP / REST upload / …) and image tooling
    # runs wherever the backend owns it.
    # ------------------------------------------------------------------

    def stage_source(
        self,
        local_path: Path,
        backend: StorageBackend,
    ) -> str:
        """Upload *local_path* to *backend* and return its backend ref.

        Images are keyed by content hash so a single source is staged
        once per backend — subsequent orchestrator runs against the
        same backend hit the upload cache.  For
        :class:`~testrange.storage.LocalStorageBackend`, when
        *local_path* already lives under the backend's cache root this
        is a no-op and returns the input path unchanged.

        :param local_path: Source image on the outer host (e.g. the
            path returned by :meth:`get_image`).
        :param backend: Destination backend.
        :returns: Backend-local ref the hypervisor can open.
        :raises CacheError: On upload or image-manipulation failure.
        """
        local_path = local_path.expanduser().resolve()
        if not local_path.is_file():
            raise CacheError(f"staging source {local_path!r}: not a file")

        # LOCAL FAST PATH: when the backend is the same filesystem as
        # ``local_path`` and the file already lives under the backend
        # cache root, skip the copy — it's already where the
        # hypervisor expects it.
        transport = backend.transport
        backend_root = transport.cache_root
        try:
            local_path.relative_to(Path(backend_root))
            return str(local_path)
        except ValueError:
            pass

        ext = local_path.suffix
        digest = _sha256_file(local_path)[:24]
        dest_ref = transport._join(
            transport.images_dir(), f"{digest}{ext}"
        )
        if transport.exists(dest_ref):
            _log.debug("backend image cache hit (%s)", digest)
            return dest_ref
        _log.info(
            "staging source image %s → %s",
            local_path.name, dest_ref,
        )
        with log_duration(
            _log, f"upload {local_path.name} to backend"
        ):
            transport.upload(local_path, dest_ref)
        return dest_ref

    # ------------------------------------------------------------------
    # Per-VM resource layout.
    #
    # Each cached VM owns a directory ``<vms_dir>/<config_hash>/`` and
    # every file belonging to that VM — primary disk, manifest,
    # any additional drives, plus whatever backend-specific blobs
    # the hypervisor wants to keep alongside them — lives inside it.
    # The cache layer only knows about the universal pair (disk +
    # manifest); backends drop their own resources via
    # :meth:`vm_resource_ref` with arbitrary names.  The
    # :doc:`HTTP cache </usage/http_cache>` URL keyspace mirrors this
    # layout under a per-orchestrator backend prefix.
    # ------------------------------------------------------------------

    MANIFEST_RESOURCE = "manifest.json"
    """Conventional filename of the build manifest.

    Universal across backends: every cached VM has a manifest
    describing how it was built.  The primary disk's filename, by
    contrast, is owned by the backend's disk format — see
    :attr:`~testrange.storage.disk.AbstractDiskFormat.primary_disk_filename`.
    """

    def vm_dir(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Return the per-VM directory inside the backend's cache.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend whose ``vms/`` dir hosts the VM.
        :returns: ``<transport.vms_dir>/<config_hash>``.
        """
        t = backend.transport
        return t._join(t.vms_dir(), config_hash)

    def vm_resource_ref(
        self,
        config_hash: str,
        resource: str,
        backend: StorageBackend,
    ) -> str:
        """Return the backend-local ref for a named resource of a
        cached VM.  Does not check existence.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param resource: Filename inside the VM's directory.  Use
            :attr:`MANIFEST_RESOURCE` for the manifest, the
            backend's disk-format
            :attr:`~testrange.storage.disk.AbstractDiskFormat.primary_disk_filename`
            for the primary disk, or any arbitrary name for
            backend-specific resources (additional disks,
            hypervisor-specific config blobs, firmware-state blobs —
            anything the backend wants to keep in the per-VM
            directory).
        :param backend: Backend whose ``vms/`` dir hosts the VM.
        :returns: ``<transport.vms_dir>/<config_hash>/<resource>``.
        """
        t = backend.transport
        return t._join(self.vm_dir(config_hash, backend), resource)

    def vm_disk_ref(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Backend-local ref for the cached primary disk.

        Filename comes from the backend's disk format
        (:attr:`~testrange.storage.disk.AbstractDiskFormat.primary_disk_filename`).
        """
        return self.vm_resource_ref(
            config_hash, backend.disk.primary_disk_filename, backend,
        )

    def vm_manifest_ref(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Convenience for ``vm_resource_ref(hash, MANIFEST_RESOURCE, backend)``."""
        return self.vm_resource_ref(config_hash, self.MANIFEST_RESOURCE, backend)

    def _remote_vm_resource_key(
        self,
        config_hash: str,
        resource: str,
    ) -> str:
        """Build the HTTP-cache URL key for a per-VM resource.

        Form: ``<backend_name>/vms/<config_hash>/<resource>``.  The
        backend prefix lets a single remote serve multiple
        hypervisor backends without artifact-format collisions —
        each backend's resources sit under a sibling subtree.
        """
        return f"{self.backend_name}/vms/{config_hash}/{resource}"

    def get_vm(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str | None:
        """Return the cached primary-disk ref for *config_hash*, or ``None``.

        A cache hit requires **both** the disk and its
        ``manifest.json`` to exist.  The manifest is written last by
        :meth:`store_vm`, so its absence is the canonical signal that
        a previous compress step crashed mid-write (OOM, SIGKILL,
        power loss) and left a truncated disk behind.  Returning
        ``None`` in that case forces a rebuild; the stale disk is
        overwritten by the next :meth:`store_vm` invocation.

        When a remote :class:`~testrange.cache_http.HttpCache` is
        configured and the local backend cache is empty, the remote is
        queried; on a remote hit both the disk and the manifest are
        pulled into the local backend and the freshly-populated ref is
        returned, exactly as if a previous run had built it locally.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend to check.
        :returns: Backend-local ref on hit, ``None`` on miss (or on a
            detected partial-write orphan).
        """
        disk_ref = self.vm_disk_ref(config_hash, backend)
        manifest_ref = self.vm_manifest_ref(config_hash, backend)
        transport = backend.transport
        if not transport.exists(disk_ref):
            if self._fill_vm_from_remote(
                config_hash, disk_ref, manifest_ref, backend,
            ):
                return disk_ref
            return None
        if not transport.exists(manifest_ref):
            _log.warning(
                "cache entry %r has a disk but no manifest — treating "
                "as partial write, will rebuild",
                config_hash,
            )
            return None
        return disk_ref

    def _fill_vm_from_remote(
        self,
        config_hash: str,
        disk_ref: str,
        manifest_ref: str,
        backend: StorageBackend,
    ) -> bool:
        """Try to populate the backend's per-VM cache directory from
        the HTTP cache.  Returns ``True`` on hit.

        Only fires for local-filesystem backends in this slice; SSH /
        remote backends would need a tmp-then-upload round-trip that
        adds bandwidth without obvious benefit (you wouldn't put the
        HTTP cache and the SSH-reached hypervisor on different sides
        of the slow link).  Logs a debug line and returns ``False``
        for those.
        """
        if self.remote is None:
            return False
        if not _transport_is_local(backend.transport):
            _log.debug(
                "http-cache: skipping fill for non-local backend %r",
                type(backend.transport).__name__,
            )
            return False

        disk_key = self._remote_vm_resource_key(
            config_hash, backend.disk.primary_disk_filename,
        )
        manifest_key = self._remote_vm_resource_key(
            config_hash, self.MANIFEST_RESOURCE,
        )
        if not self.remote.exists(disk_key):
            return False

        disk_path = Path(disk_ref)
        manifest_path = Path(manifest_ref)
        with log_duration(
            _log, f"http-cache fill VM {config_hash}",
        ):
            if not self.remote.get(disk_key, disk_path):
                return False
        if not self.remote.get(manifest_key, manifest_path):
            # Disk came down but manifest didn't — get_vm's manifest
            # check would then mark the entry as a partial write on
            # the next call.  Drop the disk so we don't pollute the
            # local cache with an orphan.
            disk_path.unlink(missing_ok=True)
            _log.warning(
                "http-cache: disk %s present but manifest missing; "
                "discarded fill",
                config_hash,
            )
            return False
        _log.info("VM %s filled from http-cache", config_hash)
        return True

    def store_vm(
        self,
        config_hash: str,
        src_ref: str,
        manifest: dict[str, Any],
        backend: StorageBackend,
    ) -> str:
        """Compress *src_ref* into the backend's per-VM cache directory.

        Uses the backend's disk-format ``compress`` op so the
        compression runs wherever the source lives (remote for SSH
        backends, local for the default).  The disk is written via a
        ``.partial`` staging path and atomically renamed on success,
        so a mid-compress crash can never leave a plausible-looking-
        but-truncated file at the final name.  The ``manifest.json``
        is written *after* the rename — its presence is what
        :meth:`get_vm` checks to distinguish a complete cache entry
        from a partial one.

        :param config_hash: Hash key for this VM configuration.
        :param src_ref: Backend-local ref to the post-install image.
        :param manifest: Build instructions; recorded verbatim in the
            cached ``manifest.json``.
        :param backend: Backend where the VM directory should land.
        :returns: Backend-local ref to the stored disk image.
        :raises CacheError: If the backend's compress step fails.
        """
        dest_ref = self.vm_disk_ref(config_hash, backend)
        partial_ref = dest_ref + ".partial"
        manifest_ref = self.vm_manifest_ref(config_hash, backend)
        transport = backend.transport
        # Make sure the per-VM directory exists before compress writes
        # into it.
        transport.makedirs(self.vm_dir(config_hash, backend))
        # A leftover .partial from an earlier crashed run would make
        # the disk-format tool refuse to overwrite (or race the
        # compress with another process); blow it away first.
        transport.remove(partial_ref)
        try:
            with log_duration(
                _log, f"compress installed image for {manifest.get('name', '?')!r}"
            ):
                backend.disk.compress(src_ref, partial_ref)
            transport.rename(partial_ref, dest_ref)
        except BaseException:
            # Compress died (SIGKILL, OOM, Ctrl-C).  Clean the stub so
            # the next run doesn't inherit a half-written artefact.
            transport.remove(partial_ref)
            raise
        transport.write_bytes(
            manifest_ref,
            json.dumps(
                manifest, indent=2, default=str, sort_keys=True,
            ).encode("utf-8"),
        )
        self._publish_vm_to_remote(config_hash, dest_ref, manifest_ref, backend)
        return dest_ref

    def _publish_vm_to_remote(
        self,
        config_hash: str,
        disk_ref: str,
        manifest_ref: str,
        backend: StorageBackend,
    ) -> None:
        """Best-effort PUT of a freshly-stored VM's disk + manifest to
        the HTTP cache.  See :meth:`_fill_vm_from_remote` for the
        local-only scope rationale.
        """
        if self.remote is None:
            return
        if not _transport_is_local(backend.transport):
            return
        disk_key = self._remote_vm_resource_key(
            config_hash, backend.disk.primary_disk_filename,
        )
        manifest_key = self._remote_vm_resource_key(
            config_hash, self.MANIFEST_RESOURCE,
        )
        with log_duration(
            _log, f"http-cache publish VM {config_hash}",
        ):
            self.remote.put(disk_key, Path(disk_ref))
        self.remote.put(manifest_key, Path(manifest_ref))


def _transport_is_local(transport: Any) -> bool:
    """True if *transport* operates on the local filesystem.

    Imported lazily to avoid a circular dependency between
    :mod:`testrange.cache` and :mod:`testrange.storage`.
    """
    from testrange.storage.transport.local import (  # noqa: PLC0415
        LocalFileTransport,
    )

    return isinstance(transport, LocalFileTransport)


def vm_config_hash(
    iso: str,
    usernames_passwords_sudo: list[tuple[str, str, bool]],
    package_reprs: list[str],
    post_install_cmds: list[str],
    disk_size: str,
) -> str:
    """Compute a deterministic hash key for a VM configuration.

    SSH keys are intentionally excluded so that key rotation does not
    invalidate the cached installed image.

    :param iso: The ``iso=`` string as passed to a VM spec.
    :param usernames_passwords_sudo: Sorted list of ``(username, password, sudo)``
        tuples for all credentials.
    :param package_reprs: Sorted list of ``repr(pkg)`` strings for all packages.
    :param post_install_cmds: Ordered list of post-install shell commands.
    :param disk_size: Normalised disk size string (e.g. ``'64G'``).
    :returns: A 24-character lowercase hex string.
    """
    canonical = {
        "iso": iso,
        "users": [
            {"u": u, "p": p, "s": s}
            for u, p, s in sorted(usernames_passwords_sudo)
        ],
        "packages": sorted(package_reprs),
        "cmds": post_install_cmds,
        "disk_size": disk_size,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()[:24]


def _sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file's contents.

    :param path: Path to the file to hash.
    :returns: 64-character lowercase hex string.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
