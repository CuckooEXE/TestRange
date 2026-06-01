"""Integration test for the sanctioned ESXi xorriso ISO-prep (ADR-0022).

Exercises ``_esxi_prepare.prepare_iso`` against a real ``xorriso`` (no live ESXi
needed): build a synthetic ESXi-shaped ISO carrying ``/BOOT.CFG`` with a
``kernelopt=`` line, run the two-pass patch, and confirm ks.cfg landed at the
root and the boot config's kernelopt was rewritten to ``runweasel
ks=cdrom:/ks.cfg``. Skips where ``xorriso`` is absent.

The runnable slice of the nested-ESXi smoke (BUILD-8); the full install to green
needs a real ESXi 8 ISO + nested KVM (BIOS/i440fx/IDE) + a bound profile.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("xorriso") is None, reason="xorriso not installed")


def _make_esxi_like_iso(path: Path) -> None:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, vol_ident="ESXI")
    bootcfg = b"bootstate=0\ntitle=Loading ESXi installer\nkernelopt=cdromBoot runweasel\n"
    iso.add_fp(io.BytesIO(bootcfg), len(bootcfg), "/BOOT.CFG;1")
    with path.open("wb") as fp:
        iso.write_fp(fp)
    iso.close()


def _read_iso_file(path: Path, iso_path: str) -> bytes:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    try:
        out = io.BytesIO()
        iso.get_file_from_iso_fp(out, iso_path=iso_path)
        return out.getvalue()
    finally:
        iso.close()


def _iso_filenames(path: Path) -> set[str]:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    try:
        names: set[str] = set()
        for _dirpath, _dirs, files in iso.walk(iso_path="/"):
            names.update(f.split(";")[0] for f in files)
        return names
    finally:
        iso.close()


def test_prepare_injects_ks_and_patches_kernelopt(tmp_path: Path) -> None:
    from testrange.builders._esxi_prepare import prepare_iso, render_kickstart

    src = tmp_path / "esxi.iso"
    out = tmp_path / "prepared.iso"
    _make_esxi_like_iso(src)
    ks = render_kickstart(root_password="VMware1!", ssh_key=None)

    prepare_iso(src, out, kickstart=ks)

    assert out.exists()
    names = _iso_filenames(out)
    assert "KS.CFG" in names or "ks.cfg" in names, f"ks.cfg not injected: {names}"
    # The boot config's kernelopt was rewritten and the original cdromBoot dropped.
    patched = _read_iso_file(out, "/BOOT.CFG;1").decode("utf-8", "replace")
    assert "ks=cdrom:/ks.cfg" in patched, patched
    assert "cdromBoot" not in patched, patched


def test_render_kickstart_rejects_empty_password() -> None:
    from testrange.builders._esxi_prepare import EsxiPrepareError, render_kickstart

    with pytest.raises(EsxiPrepareError, match="non-empty root_password"):
        render_kickstart(root_password="", ssh_key=None)
