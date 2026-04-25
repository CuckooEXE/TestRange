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
    │   ├── <url_hash>.qcow2          # or .img
    │   └── <url_hash>.meta.json      # URL, size, sha256, timestamp
    └── vms/
        ├── <config_hash>.qcow2       # compressed post-install disk
        └── <config_hash>.json        # instructions that built it

Each ``vms/<config_hash>.json`` records the exact set of modifications
applied to the base image (packages, user accounts, post-install shell
commands, target disk size).  Inspect it with any JSON viewer to see
what a cached VM image contains without booting it.

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


class CacheManager:
    """Manages the TestRange disk-image cache.

    Only two kinds of artefact live here: downloaded base OS images, and
    compressed post-install VM snapshots.  Ephemeral per-run scratch
    files belong to :class:`~testrange._run.RunDir` instead.

    :param root: Base directory for all cached data.  Defaults to
        ``/var/tmp/testrange/<user>`` (overridable via ``TESTRANGE_CACHE_DIR``).
    """

    root: Path
    """Root directory of the cache (e.g. ``/var/tmp/testrange/alice``)."""

    images_dir: Path
    """Subdirectory holding downloaded base OS images (``<root>/images``)."""

    vms_dir: Path
    """Subdirectory holding post-install VM snapshots (``<root>/vms``)."""

    def __init__(self, root: Path = _DEFAULT_CACHE_ROOT) -> None:
        self.root = root
        self.images_dir = root / "images"
        self.vms_dir = root / "vms"
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
        """Return the local path for a cloud image, downloading if necessary.

        Downloads are streamed with a progress bar.  A ``.meta.json``
        sidecar records the source URL, download timestamp, and SHA-256
        of the file content for integrity verification on subsequent
        cache hits.

        :param url: An ``https://`` URL pointing to a ``.qcow2`` or
            ``.img`` cloud image.
        :returns: Path to the locally cached image file.
        :raises ImageNotFoundError: If the download fails (HTTP error,
            network timeout, etc.).
        :raises CacheError: If the file cannot be written to the cache.
        """
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:24]
        meta_path = self.images_dir / f"{url_hash}.meta.json"
        ext = ".qcow2" if url.endswith(".qcow2") else ".img"
        image_path = self.images_dir / f"{url_hash}{ext}"

        lock_path = self.images_dir / f"{url_hash}.lock"
        with FileLock(str(lock_path), timeout=1800):
            if image_path.exists() and meta_path.exists():
                _log.debug("base image cache hit for %s", url)
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
        return image_path

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

    def get_proxmox_prepared_iso(self, vanilla_iso: Path) -> Path:
        """Return the local path to a prepared ProxMox installer ISO.

        The ProxMox VE installer only enters unattended (answer-file)
        mode when its initrd carries ``/auto-installer-mode.toml``.  We
        produce that modified ISO with a pure-Python replacement of
        ``proxmox-auto-install-assistant prepare-iso`` — see
        :mod:`testrange.vms.builders._proxmox_prepare` — and cache the
        output here keyed by the SHA-256 of the vanilla ISO.  One base
        ISO version → one prepared copy, reused across every ProxMox
        VM that builds against that source.

        Concurrent callers contend on a :class:`FileLock` sibling;
        whoever wins writes the prepared ISO atomically via a
        ``.part`` rename, so an interrupted prep can't leave a stub
        that later callers mistake for a cache hit.

        :param vanilla_iso: Local path to an unmodified PVE installer
            ISO (typically the result of :meth:`get_image` for a
            ProxMox VE release URL).
        :returns: Path to the prepared ISO under
            ``<cache_root>/images/proxmox-prepared-<sha>.iso``.
        :raises CacheError: On filesystem errors during prep.
        :raises ~testrange.vms.builders._proxmox_prepare.ProxmoxPrepareError:
            If the ISO can't be prepared (unknown initrd compression,
            missing initrd, etc.).
        """
        from testrange.vms.builders._proxmox_prepare import prepare_iso_bytes

        sha = _sha256_file(vanilla_iso)[:24]
        dest = self.images_dir / f"proxmox-prepared-{sha}.iso"
        lock_path = self.images_dir / f"proxmox-prepared-{sha}.lock"
        with FileLock(str(lock_path), timeout=1800):
            if dest.exists():
                _log.debug("proxmox prepared-ISO cache hit (%s)", sha)
                return dest
            _log.info(
                "preparing proxmox installer ISO (sha=%s) → %s", sha, dest,
            )
            tmp = dest.with_suffix(".iso.part")
            try:
                with log_duration(
                    _log, f"prepare proxmox ISO {vanilla_iso.name!r}"
                ):
                    prepare_iso_bytes(vanilla_iso, tmp)
                os.chmod(tmp, 0o644)
                tmp.rename(dest)
            except BaseException:
                if tmp.exists():
                    tmp.unlink()
                raise
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

        ext = local_path.suffix or ".qcow2"
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

    def vm_snapshot_ref(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Return the backend-local ref where *config_hash*'s snapshot
        would live.  Does not check existence — see :meth:`get_vm`.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend whose ``vms/`` dir hosts the snapshot.
        :returns: ``<transport.vms_dir>/<config_hash>.qcow2``.
        """
        t = backend.transport
        return t._join(t.vms_dir(), f"{config_hash}.qcow2")

    def vm_manifest_ref(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Return the backend-local ref for the manifest sidecar.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend whose ``vms/`` dir hosts the manifest.
        :returns: ``<transport.vms_dir>/<config_hash>.json``.
        """
        t = backend.transport
        return t._join(t.vms_dir(), f"{config_hash}.json")

    def vm_nvram_ref(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str:
        """Return the backend-local ref for a cached UEFI NVRAM sidecar.

        Lives alongside the qcow2 / manifest at
        ``<vms_dir>/<config_hash>.nvram.fd``.  Absence means this VM
        was installed in BIOS mode (no NVRAM to preserve) or nothing
        has populated it yet for this hash.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend whose ``vms/`` dir hosts the sidecar.
        """
        t = backend.transport
        return t._join(t.vms_dir(), f"{config_hash}.nvram.fd")

    def store_vm_nvram(
        self,
        config_hash: str,
        nvram_src_ref: str,
        backend: StorageBackend,
    ) -> str:
        """Snapshot the install-phase UEFI NVRAM into the cache sidecar.

        The installer writes EFI boot entries into NVRAM during
        install; without this snapshot those entries die with the
        per-run NVRAM file (libvirt's
        ``VIR_DOMAIN_UNDEFINE_NVRAM`` flag deletes it at teardown),
        and run-phase UEFI comes up with an empty ``BootOrder``.
        For distros that don't also write the removable-path
        fallback (``/EFI/BOOT/BOOTX64.EFI``) that leaves the VM
        hanging at an empty EFI shell.

        Atomic: writes to a ``.partial`` sibling and renames on
        success, matching :meth:`store_vm`'s discipline.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param nvram_src_ref: Backend-local ref to the install-phase
            NVRAM file (typically ``<run>/<vm>_VARS.fd``).
        :param backend: Backend to store the sidecar on.
        :returns: Backend-local ref to the stored NVRAM sidecar.
        :raises CacheError: If the read / write fails.
        """
        dest_ref = self.vm_nvram_ref(config_hash, backend)
        partial_ref = dest_ref + ".partial"
        transport = backend.transport
        transport.makedirs(transport.vms_dir())
        transport.remove(partial_ref)
        try:
            data = transport.read_bytes(nvram_src_ref)
            transport.write_bytes(partial_ref, data)
            transport.rename(partial_ref, dest_ref)
        except BaseException:
            transport.remove(partial_ref)
            raise
        return dest_ref

    def get_vm_nvram(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str | None:
        """Return the cached NVRAM ref for *config_hash*, or ``None``.

        Callers on the run-phase lifecycle use this to seed the
        per-run NVRAM file from the install-phase snapshot instead
        of the empty global OVMF_VARS template.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend to check.
        """
        ref = self.vm_nvram_ref(config_hash, backend)
        if backend.transport.exists(ref):
            return ref
        return None

    def get_vm(
        self,
        config_hash: str,
        backend: StorageBackend,
    ) -> str | None:
        """Return the cached snapshot ref for *config_hash*, or ``None``.

        A cache hit requires **both** the qcow2 and its ``.json``
        manifest to exist.  The manifest is written last by
        :meth:`store_vm`, so its absence is the canonical signal that
        a previous compress step crashed mid-write (OOM, SIGKILL,
        power loss) and left a truncated qcow2 behind.  Returning
        ``None`` in that case forces a rebuild; the stale qcow2 is
        overwritten by the next :meth:`store_vm` invocation.

        :param config_hash: Hash key from :func:`vm_config_hash`.
        :param backend: Backend to check.
        :returns: Backend-local ref on hit, ``None`` on miss (or on a
            detected partial-write orphan).
        """
        snapshot_ref = self.vm_snapshot_ref(config_hash, backend)
        manifest_ref = self.vm_manifest_ref(config_hash, backend)
        transport = backend.transport
        if not transport.exists(snapshot_ref):
            return None
        if not transport.exists(manifest_ref):
            _log.warning(
                "cache entry %r has a qcow2 but no manifest — treating "
                "as partial write, will rebuild",
                config_hash,
            )
            return None
        return snapshot_ref

    def store_vm(
        self,
        config_hash: str,
        src_ref: str,
        manifest: dict[str, Any],
        backend: StorageBackend,
    ) -> str:
        """Compress *src_ref* into the backend's snapshot cache.

        Uses the backend's disk-format ``compress`` op so the
        compression runs wherever the source lives (remote for SSH
        backends, local for the default).  The qcow2 is written via a
        ``.qcow2.partial`` staging path and atomically renamed on
        success, so a mid-compress crash can never leave a
        plausible-looking-but-truncated file at the final name.  A
        sibling manifest JSON is written *after* the rename — its
        presence is what :meth:`get_vm` checks to distinguish a
        complete cache entry from a partial one.

        :param config_hash: Hash key for this VM configuration.
        :param src_ref: Backend-local ref to the post-install image.
        :param manifest: Build instructions; recorded verbatim in the
            ``.json`` sidecar.
        :param backend: Backend where the snapshot should land.
        :returns: Backend-local ref to the stored snapshot.
        :raises CacheError: If the backend's compress step fails.
        """
        dest_ref = self.vm_snapshot_ref(config_hash, backend)
        partial_ref = dest_ref + ".partial"
        manifest_ref = self.vm_manifest_ref(config_hash, backend)
        transport = backend.transport
        # Make sure the target dir exists before compress writes into it.
        transport.makedirs(transport.vms_dir())
        # A leftover .partial from an earlier crashed run would make
        # qemu-img refuse to overwrite (or race the compress with
        # another process); blow it away first.
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
        return dest_ref


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
