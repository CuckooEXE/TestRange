"""No-op :class:`~testrange.vms.builders.base.Builder` for BYOI images.

Used when the caller hands a VM spec a qcow2 that is already fully
provisioned (produced by Packer, a custom build pipeline, or a
hand-prepared golden image).  There is no install phase —
:meth:`NoOpBuilder.needs_install_phase` returns ``False`` — so the
backend's ``build()`` calls :meth:`~Builder.ready_image` and skips
straight to creating a run overlay on the staged disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from testrange._logging import get_logger
from testrange._qemu_img import info as _qemu_img_info
from testrange.exceptions import VMBuildError
from testrange.vms.builders.base import Builder, RunDomain

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.cache import CacheManager
    from testrange.vms.base import AbstractVM as VM


_log = get_logger(__name__)


class NoOpBuilder(Builder):
    """Prebuilt-qcow2 strategy.  Skips the install phase and copies the
    user's qcow2 into the cache under a content-hashed name.

    :param windows: If ``True``, the prebuilt image is a Windows guest.
        Propagates to the run-phase domain XML (UEFI + SATA primary
        disk + e1000e NIC) and changes the default communicator to
        ``"winrm"``.  Defaults to ``False``.
    """

    windows: bool
    """Whether the staged image is a Windows guest."""

    def __init__(self, windows: bool = False) -> None:
        self.windows = windows

    def default_communicator(self) -> str:
        return "winrm" if self.windows else "guest-agent"

    def needs_install_phase(self) -> bool:
        return False

    def cache_key(self, vm: VM) -> str:
        """Not applicable — NoOp keys the staged disk on content hash
        inside :meth:`ready_image`.  Callers must check
        :meth:`needs_install_phase` first.
        """
        raise NotImplementedError(
            "NoOpBuilder does not use the install-phase cache; call "
            "ready_image() instead."
        )

    def prepare_install_domain(
        self,
        vm: VM,
        run: RunDir,
        cache: CacheManager,
    ) -> Any:
        """Not applicable.  Guarded by :meth:`needs_install_phase`."""
        raise NotImplementedError(
            "NoOpBuilder has no install phase; prepare_install_domain "
            "should never be called."
        )

    def install_manifest(
        self,
        vm: VM,
        config_hash: str,
    ) -> dict[str, Any]:
        """Not applicable.  Guarded by :meth:`needs_install_phase`."""
        raise NotImplementedError(
            "NoOpBuilder does not produce an install manifest."
        )

    def prepare_run_domain(
        self,
        vm: VM,
        run: RunDir,
        mac_ip_pairs: list[tuple[str, str, str, str]],
    ) -> RunDomain:
        """Windows prebuilt images need UEFI + SATA/e1000e device
        models; Linux ones run on the default SeaBIOS + virtio chain.
        No seed ISO in either case — cloud-init / autounattend already
        ran elsewhere.
        """
        return RunDomain(
            seed_iso=None,
            uefi=self.windows,
            windows=self.windows,
        )

    def ready_image(
        self, vm: VM, cache: CacheManager, run: RunDir,
    ) -> str:
        """Stage the user-supplied qcow2 into the backend's ``vms/``
        cache and return its backend-local ref.

        Validates the source on the outer host (``qemu-img info``),
        then stages into ``<backend.vms_dir>/byoi-<sha[:24]>.qcow2``
        with a manifest JSON sidecar.  For the default local backend
        this is a filesystem copy (identical to the pre-backend
        behaviour); for SSH / remote backends it's an SFTP upload to
        the same logical location on the remote host.  Sources that
        already live under the local backend's cache root are returned
        in place.

        :raises VMBuildError: If the source does not exist, is not a
            qcow2, or if the staging copy fails.
        """
        try:
            src = Path(vm.iso).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise VMBuildError(
                f"VM {vm.name!r}: prebuilt image {vm.iso!r} not found."
            ) from exc

        try:
            meta = _qemu_img_info(src)
        except Exception as exc:  # noqa: BLE001 — surface as VMBuildError
            raise VMBuildError(
                f"VM {vm.name!r}: qemu-img info failed on {src}: {exc}"
            ) from exc
        if meta.get("format") != "qcow2":
            raise VMBuildError(
                f"VM {vm.name!r}: prebuilt image {src} is not qcow2 "
                f"(qemu-img reports format={meta.get('format')!r})."
            )

        backend = run.storage

        # Local fast path — source already under this backend's cache
        # root on a local filesystem.  Return it unchanged so repeated
        # runs don't churn the cache with identical-content copies.
        backend_root = Path(backend.cache_root)
        try:
            src.relative_to(backend_root)
            _log.info(
                "VM %r prebuilt image %s already under backend cache root; "
                "reusing in place",
                vm.name, src,
            )
            return str(src)
        except ValueError:
            pass

        sha = _sha256_file(src)[:24]
        dest_ref = backend._join(backend.vms_dir(), f"byoi-{sha}.qcow2")
        manifest_ref = backend._join(backend.vms_dir(), f"byoi-{sha}.json")

        if backend.exists(dest_ref):
            _log.info(
                "VM %r prebuilt cache hit (byoi-%s) — reusing staged copy",
                vm.name, sha,
            )
            return dest_ref

        _log.info(
            "VM %r prebuilt cache miss (byoi-%s) — staging %s into backend",
            vm.name, sha, src,
        )
        backend.makedirs(backend.vms_dir())
        backend.upload(src, dest_ref)
        backend.write_bytes(
            manifest_ref,
            json.dumps(
                {
                    "name": vm.name,
                    "source_path": str(src),
                    "sha256": sha,
                    "prebuilt": True,
                    "windows": self.windows,
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
        return dest_ref


def _sha256_file(path: Path) -> str:
    """Return a 24-char SHA-256 prefix over *path*'s bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


__all__ = ["NoOpBuilder"]
