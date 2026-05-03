"""Disk-format conversion scaffolding for cross-backend image staging.

Slice 4 of the nested-build refactor: structural readiness for
non-qcow2 backends.  Today's two backends (libvirt + Proxmox) are
qcow2-native — every install lands as a qcow2, every clone reads
qcow2.  When ESXi (vmdk + OVA), Hyper-V (vhdx), or a bare-metal-
restore backend (raw) lands, :meth:`Builder.adopt_prebuilt` (Slice
2.5) needs a way to convert a bare-metal-built qcow2 into the
target backend's format before importing.  This module provides
that abstraction.

The contract
------------

:class:`DiskFormatConverter` takes ``(src_ref, src_format,
dst_format)`` and returns a ref to a disk in *dst_format*.  When
*src_format == dst_format*, the converter MUST short-circuit to
returning *src_ref* unchanged — caching by content hash means
identity conversions never copy bytes.

Concrete implementations
------------------------

* :class:`IdentityConverter` — qcow2 → qcow2 only.  Lives here
  rather than in a backend module because every backend's adoption
  path needs it as the no-op base case.

* :class:`QemuImgConverter` — qcow2 ↔ vmdk / raw / vdi via
  ``qemu-img convert``.  Stub today (raises
  :class:`NotImplementedError`); wiring lands when an ESXi or
  raw-restore backend needs it.  Identity short-circuits the same
  way :class:`IdentityConverter` does, so call sites can use
  ``QemuImgConverter()`` unconditionally without branching on
  format.

* OVA / OVF bundles (VMware) — would land as a separate
  ``OvfToolConverter``.  Out of scope for this slice; ``ovftool``
  is a proprietary VMware download with terms-of-use that probably
  rule out making it a hard dep.

The cache layout
----------------

When non-identity conversions land, the cache layout extends from
``<root>/vms/<config_hash>/disk.qcow2`` to
``<root>/vms/<config_hash>/disk.<fmt>``, with each format computed
on demand from the canonical qcow2.  The
:meth:`AbstractDiskFormat.primary_disk_filename` indirection that
:class:`~testrange.cache.CacheManager` already uses (see
``cache.vm_disk_ref``) is what makes per-format file naming
backwards-compatible — backends that stay qcow2-only see no change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DiskFormatConverter(ABC):
    """Abstract converter from one disk format to another.

    Concrete implementations may either:

    * Convert in place (return a new ref, leaving *src_ref* alone),
      or
    * Short-circuit identity (return *src_ref* unchanged when
      *src_format == dst_format*).

    Every implementation MUST honour the identity contract — call
    sites assume ``convert(ref, fmt, fmt) == ref`` so they don't
    have to special-case the format-matches branch.

    Refs are backend-local strings whose interpretation is the
    transport's call.  For local-fs backends they're absolute paths;
    for remote (SSH / REST) backends they're whatever the transport
    accepts as a ``read_bytes`` argument.  This module doesn't move
    bytes itself; it produces (or returns) a ref for an artifact in
    *dst_format* that the calling backend can consume.
    """

    @abstractmethod
    def convert(
        self,
        *,
        src_ref: str,
        src_format: str,
        dst_format: str,
    ) -> str:
        """Convert *src_ref* (in *src_format*) to *dst_format* and
        return a ref to the result.

        :param src_ref: Backend-local ref to the source disk.
        :param src_format: Format identifier (``"qcow2"`` /
            ``"vmdk"`` / ``"raw"`` / …).
        :param dst_format: Format identifier the caller needs.
        :returns: Backend-local ref to a disk in *dst_format*.
        :raises ValueError: If the (src, dst) format pair is not
            supported by this converter.
        :raises NotImplementedError: For pairs whose support is
            scaffolded but not yet wired (e.g. qcow2→vmdk via
            qemu-img while the call site doesn't need it).
        """


class IdentityConverter(DiskFormatConverter):
    """No-op converter for the qcow2 → qcow2 case.

    Implementing the contract as its own class (rather than as a
    branch inside :class:`QemuImgConverter`) makes the cross-backend
    cache layer's "if formats match, no work needed" decision
    explicit — call sites that *only* support qcow2 (today's
    libvirt + Proxmox backends) instantiate :class:`IdentityConverter`
    and never touch the qemu-img path.
    """

    def convert(
        self,
        *,
        src_ref: str,
        src_format: str,
        dst_format: str,
    ) -> str:
        if src_format != dst_format or src_format != "qcow2":
            raise ValueError(
                f"IdentityConverter only handles qcow2→qcow2; got "
                f"{src_format!r} → {dst_format!r}.  Use "
                "QemuImgConverter or a format-specific converter."
            )
        return src_ref


class QemuImgConverter(DiskFormatConverter):
    """``qemu-img convert``-backed converter for qcow2 ↔ vmdk / raw / vdi.

    Today the stub raises :class:`NotImplementedError` for every
    non-identity pair — wiring lands when a backend needs it.
    Identity (qcow2 → qcow2) short-circuits to
    :class:`IdentityConverter`'s contract so call sites can use this
    class unconditionally.
    """

    def convert(
        self,
        *,
        src_ref: str,
        src_format: str,
        dst_format: str,
    ) -> str:
        if src_format == dst_format == "qcow2":
            return src_ref
        # Future wiring: shell out to ``qemu-img convert -f
        # {src_format} -O {dst_format} {src_ref} {dst_ref}``, derive
        # ``dst_ref`` from ``src_ref`` + a dst-format suffix, and
        # cache the result alongside the source.  For now, fail loud
        # so a backend that calls into this without expecting the
        # raise gets a clear message instead of corrupted state.
        raise NotImplementedError(
            f"qemu-img convert {src_format} → {dst_format} is "
            "scaffolded but not yet wired.  Add the implementation "
            "in testrange/_disk_format.py when the calling backend "
            "(ESXi for vmdk; bare-metal-restore for raw) lands."
        )


__all__ = [
    "DiskFormatConverter",
    "IdentityConverter",
    "QemuImgConverter",
]
