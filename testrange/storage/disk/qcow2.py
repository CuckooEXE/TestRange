"""qcow2 operations via ``qemu-img``.

Every method is a single ``qemu-img`` invocation routed through the
transport's :meth:`~AbstractFileTransport.run_tool` — runs locally
against :class:`LocalFileTransport`, remotely against
:class:`SSHFileTransport`, or anywhere else the transport reaches.

The format knows nothing about transports beyond "here's an argv, run
it."  That's the whole separation-of-concerns benefit of the two-axis
decomposition: QEMU-specific tool arguments live here; transport
specifics live in the transport.
"""

from __future__ import annotations

from testrange.exceptions import CacheError
from testrange.storage.disk.base import AbstractDiskFormat

_FAST_OP_TIMEOUT = 60.0
"""Seconds for near-instant metadata ops (create / resize).

qcow2 create and resize touch header bytes only; 60s is generous and
still surfaces a wedged transport quickly."""

_CONVERT_TIMEOUT = 1800.0
"""Seconds for ``qemu-img convert -c`` on a post-install snapshot.

Compression is O(disk size).  A 40 GB Windows install compresses in
a few minutes on modern hardware, but can take much longer on slow
disks, under CPU contention, or on remote transports where the
working disk is also being uploaded.  30 minutes matches the
install-phase timeout on the caller side — a compression that
exceeds it is a real stall, not a normal tail."""


class Qcow2DiskFormat(AbstractDiskFormat):
    """``qemu-img``-based qcow2 operations.

    The qcow2 format ships with QEMU and is what the libvirt / KVM
    family expects; Proxmox storage pools serve it natively too.
    """

    def create_overlay(self, backing_ref: str, dest_ref: str) -> None:
        self._run([
            "qemu-img", "create",
            "-f", "qcow2",
            "-b", backing_ref,
            "-F", "qcow2",
            dest_ref,
        ])

    def create_blank(self, dest_ref: str, size: str) -> None:
        self._run(["qemu-img", "create", "-f", "qcow2", dest_ref, size])

    def resize(self, ref: str, size: str) -> None:
        self._run(["qemu-img", "resize", ref, size])

    def compress(self, src_ref: str, dest_ref: str) -> None:
        # ``convert -c`` emits a compressed qcow2 — read-only but
        # significantly smaller; fine for an archived post-install
        # snapshot that run-phase overlays will sit on top of.
        #
        # Compression scales with disk size.  The 60s default on
        # ``run_tool`` was falling short for ordinary 10-40 GB images
        # and surfacing as a confusing "qemu-img timed out" after the
        # install phase had already succeeded.
        self._run(
            [
                "qemu-img", "convert",
                "-f", "qcow2",
                "-O", "qcow2",
                "-c",
                src_ref,
                dest_ref,
            ],
            timeout=_CONVERT_TIMEOUT,
        )

    def _run(self, argv: list[str], timeout: float = _FAST_OP_TIMEOUT) -> None:
        """Execute *argv* via the transport; raise on non-zero exit.

        Isolated so every method here reads identically and the
        error-wrapping logic lives in one place.  Callers override
        ``timeout`` for ops whose runtime scales with disk size —
        :meth:`compress` is the only one in that bucket today.
        """
        code, _, stderr = self._transport.run_tool(argv, timeout=timeout)
        if code != 0:
            # ``argv[:2]`` is e.g. ``['qemu-img', 'convert']`` — enough
            # context to pick out which op failed from a multi-step
            # pipeline.
            op = " ".join(argv[:2])
            raise CacheError(
                f"{op} failed "
                f"(exit {code}): "
                f"{stderr.decode(errors='replace').strip()}"
            )
