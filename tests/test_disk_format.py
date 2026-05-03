"""Unit tests for the :mod:`testrange._disk_format` scaffolding.

Slice 4 of the nested-build refactor: pure structural readiness for
non-qcow2 backends (ESXi vmdk, Hyper-V vhdx, OVA bundles for
template marketplaces).  The conversion path itself is unwired today
— libvirt + Proxmox are both qcow2-native, so identity is the only
exercised case.  These tests pin the ABC contract + identity impl +
the qemu-img stub's error shape so a future ESXi backend can wire
the real conversions without surprises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange._disk_format import (
    DiskFormatConverter,
    IdentityConverter,
    QemuImgConverter,
)


class TestDiskFormatConverterContract:
    """Every concrete converter must satisfy the ABC."""

    @pytest.mark.parametrize("converter_cls", [
        IdentityConverter,
        QemuImgConverter,
    ])
    def test_is_a_subclass(self, converter_cls: type) -> None:
        assert issubclass(converter_cls, DiskFormatConverter)


class TestIdentityConverter:
    """qcow2 → qcow2 (the today's-only-real case): the converter is a
    no-op that returns the source ref unchanged.  Caching by content
    hash means we don't even copy the bytes — identity passes through."""

    def test_returns_source_ref_unchanged(self, tmp_path: Path) -> None:
        src = tmp_path / "disk.qcow2"
        src.write_bytes(b"fake-qcow2")
        result = IdentityConverter().convert(
            src_ref=str(src), src_format="qcow2", dst_format="qcow2",
        )
        assert result == str(src)

    def test_handles_format_mismatch_via_raise(
        self, tmp_path: Path,
    ) -> None:
        # Identity converter is qcow2→qcow2 only.  Any other src_format
        # / dst_format combo is a programming error — raise loudly so
        # the call site uses :class:`QemuImgConverter` (or a future
        # ovftool-backed one) for non-identity cases.
        src = tmp_path / "disk.vmdk"
        src.write_bytes(b"")
        with pytest.raises(ValueError, match="qcow2"):
            IdentityConverter().convert(
                src_ref=str(src), src_format="vmdk", dst_format="qcow2",
            )


class TestQemuImgConverter:
    """``qemu-img convert`` is the workhorse for qcow2 ↔ vmdk and
    qcow2 ↔ raw.  Today the stub raises :class:`NotImplementedError`
    — wiring is deferred until an ESXi (vmdk-target) or bare-metal-
    restore (raw-target) backend lands."""

    def test_raises_not_implemented_for_now(
        self, tmp_path: Path,
    ) -> None:
        src = tmp_path / "disk.qcow2"
        src.write_bytes(b"")
        with pytest.raises(NotImplementedError, match="qemu-img"):
            QemuImgConverter().convert(
                src_ref=str(src), src_format="qcow2", dst_format="vmdk",
            )

    def test_identity_short_circuits_to_unchanged_ref(
        self, tmp_path: Path,
    ) -> None:
        # Even though the qcow2→vmdk path raises, qcow2→qcow2 must
        # short-circuit to identity — same contract as
        # :class:`IdentityConverter`.  Lets the call site use
        # ``QemuImgConverter`` unconditionally without branching.
        src = tmp_path / "disk.qcow2"
        src.write_bytes(b"fake")
        result = QemuImgConverter().convert(
            src_ref=str(src), src_format="qcow2", dst_format="qcow2",
        )
        assert result == str(src)
