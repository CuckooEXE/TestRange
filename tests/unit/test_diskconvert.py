"""CORE-2: sanctioned qemu-img qcow2<->vmdk conversion (ADR-0024).

Real conversions against the host ``qemu-img`` (a tiny image round-trips in
milliseconds); skipped where the binary is absent so the unit gate never couples
to a host binary. The absence path (``require_qemu_img``) is tested without it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.drivers import _diskconvert
from testrange.exceptions import DriverError

_HAVE_QEMU_IMG = _diskconvert.qemu_img_path() is not None
_needs_qemu = pytest.mark.skipif(not _HAVE_QEMU_IMG, reason="qemu-img not on PATH")

# qcow2 magic ("QFI\xfb"); vmdk sparse-extent magic ("KDMV", little-endian).
_QCOW2_MAGIC = b"QFI\xfb"
_VMDK_SPARSE_MAGIC = b"KDMV"


def _seed_qcow2(tmp_path: Path) -> Path:
    """A small qcow2 carved from a raw seed via the module under test."""
    raw = tmp_path / "seed.raw"
    raw.write_bytes(b"testrange-core2\n" + b"\x00" * (1024 * 1024))  # ~1 MiB
    return _diskconvert.convert(raw, tmp_path / "seed.qcow2", out_format="qcow2", in_format="raw")


class TestRequireQemuImg:
    def test_missing_binary_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_diskconvert, "qemu_img_path", lambda: None)
        with pytest.raises(DriverError, match="qemu-img not found"):
            _diskconvert.require_qemu_img()

    @_needs_qemu
    def test_present_binary_resolves(self) -> None:
        assert _diskconvert.require_qemu_img()


@_needs_qemu
class TestConversion:
    def test_qcow2_seed_has_magic(self, tmp_path: Path) -> None:
        qcow2 = _seed_qcow2(tmp_path)
        assert qcow2.read_bytes()[:4] == _QCOW2_MAGIC

    @pytest.mark.parametrize("subformat", ["streamOptimized", "monolithicSparse"])
    def test_qcow2_to_vmdk(self, tmp_path: Path, subformat: str) -> None:
        qcow2 = _seed_qcow2(tmp_path)
        vmdk = _diskconvert.qcow2_to_vmdk(qcow2, tmp_path / "out.vmdk", subformat=subformat)
        assert vmdk.exists() and vmdk.stat().st_size > 0
        # Both vmdk sparse subformats carry the KDMV sparse-extent header.
        assert vmdk.read_bytes()[:4] == _VMDK_SPARSE_MAGIC

    def test_round_trip_back_to_qcow2(self, tmp_path: Path) -> None:
        qcow2 = _seed_qcow2(tmp_path)
        vmdk = _diskconvert.qcow2_to_vmdk(qcow2, tmp_path / "mid.vmdk")
        back = _diskconvert.vmdk_to_qcow2(vmdk, tmp_path / "back.qcow2")
        assert back.read_bytes()[:4] == _QCOW2_MAGIC

    def test_missing_source_fails_loud(self, tmp_path: Path) -> None:
        with pytest.raises(DriverError, match="does not exist"):
            _diskconvert.convert(tmp_path / "nope.qcow2", tmp_path / "x.vmdk", out_format="vmdk")
