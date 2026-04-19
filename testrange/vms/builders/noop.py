"""No-op :class:`~testrange.vms.builders.base.Builder` for BYOI images.

Used when the caller hands :class:`~testrange.backends.libvirt.VM` a
qcow2 that is already fully provisioned (produced by Packer, a custom
build pipeline, or a hand-prepared golden image).  There is no
install phase — :meth:`NoOpBuilder.needs_install_phase` returns
``False`` — so :meth:`~testrange.backends.libvirt.VM.build` calls
:meth:`~Builder.ready_image` and skips straight to creating a run
overlay on the staged disk.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock

from testrange import _qemu_img
from testrange._logging import get_logger, log_duration
from testrange.cache import _sha256_file
from testrange.exceptions import VMBuildError
from testrange.vms.builders.base import Builder, RunDomain

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.backends.libvirt.vm import VM
    from testrange.cache import CacheManager


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

    def ready_image(self, vm: VM, cache: CacheManager) -> Path:
        """Stage the user-supplied qcow2 into the cache under a stable,
        content-hashed name.

        Content-hashes the source and copies it into
        ``<cache_root>/vms/byoi-<sha256[:24]>.qcow2`` on first use;
        subsequent VMs with the same file hit the staged copy.  Files
        already under the cache root are returned unchanged.

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
            meta = _qemu_img.info(src)
        except Exception as exc:  # noqa: BLE001 — surface as VMBuildError
            raise VMBuildError(
                f"VM {vm.name!r}: qemu-img info failed on {src}: {exc}"
            ) from exc
        if meta.get("format") != "qcow2":
            raise VMBuildError(
                f"VM {vm.name!r}: prebuilt image {src} is not qcow2 "
                f"(qemu-img reports format={meta.get('format')!r})."
            )

        try:
            src.relative_to(cache.root)
            _log.info(
                "VM %r prebuilt image %s is already under cache root; reusing",
                vm.name, src,
            )
            return src
        except ValueError:
            pass

        sha = _sha256_file(src)[:24]
        dest = cache.vms_dir / f"byoi-{sha}.qcow2"
        manifest = cache.vms_dir / f"byoi-{sha}.json"
        lock = FileLock(str(dest) + ".lock", timeout=1800)
        with lock:
            if dest.exists():
                _log.info(
                    "VM %r prebuilt cache hit (byoi-%s) — reusing staged copy",
                    vm.name, sha,
                )
                return dest
            _log.info(
                "VM %r prebuilt cache miss (byoi-%s) — staging %s into cache",
                vm.name, sha, src,
            )
            tmp = dest.with_suffix(".part")
            with log_duration(
                _log, f"copy prebuilt image for {vm.name!r}"
            ):
                shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o644)
            tmp.rename(dest)
            manifest.write_text(
                json.dumps(
                    {
                        "name": vm.name,
                        "source_path": str(src),
                        "sha256": _sha256_file(dest),
                        "prebuilt": True,
                        "windows": self.windows,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        return dest


__all__ = ["NoOpBuilder"]
