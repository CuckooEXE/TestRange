"""Integration test for the sanctioned xorriso ISO-prep module (ADR-0022).

Exercises ``_proxmox_prepare.prepare_iso`` against a real ``xorriso`` (no live
Proxmox/libvirt needed): build a small ISO, inject the auto-installer activation
file + first-boot script, and confirm both land at the ISO root. Skips where
``xorriso`` is absent, so it is safe in the normal suite.

This is the runnable slice of the nested-PVE smoke (BUILD-13); the full
installer-origin build to green needs a real PVE 9 ISO + nested KVM + a bound
``libvirt-local`` profile, which CI doesn't carry.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("xorriso") is None, reason="xorriso not installed")


def _make_source_iso(path: Path) -> None:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="SOURCE")
    payload = b"hello\n"
    iso.add_fp(io.BytesIO(payload), len(payload), "/HELLO.TXT;1", rr_name="hello.txt")
    with path.open("wb") as fp:
        iso.write_fp(fp)
    iso.close()


def _iso_filenames(path: Path) -> set[str]:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    try:
        names: set[str] = set()
        for _dirpath, _dirs, files in iso.walk(rr_path="/"):
            names.update(files)
        return names
    finally:
        iso.close()


def _make_source_iso_with_grub(path: Path) -> None:
    """A source ISO carrying a minimal PVE-shaped ``/boot/grub/grub.cfg`` with the
    automated menuentry, so the serial-console rewrite has something to bite on."""
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="SOURCE")
    iso.add_directory("/BOOT", rr_name="boot")
    iso.add_directory("/BOOT/GRUB", rr_name="grub")
    cfg = (
        b"menuentry 'Install Proxmox VE (Automated)' {\n"
        b"    linux /boot/linux26 ro rw quiet splash=silent proxmox-start-auto-installer\n"
        b"    initrd /boot/initrd.img\n"
        b"}\n"
    )
    iso.add_fp(io.BytesIO(cfg), len(cfg), "/BOOT/GRUB/GRUB.CFG;1", rr_name="grub.cfg")
    with path.open("wb") as fp:
        iso.write_fp(fp)
    iso.close()


def _read_iso_file(path: Path, rr_path: str) -> bytes:
    pycdlib = pytest.importorskip("pycdlib")
    iso = pycdlib.PyCdlib()
    iso.open(str(path))
    try:
        out = io.BytesIO()
        iso.get_file_from_iso_fp(out, rr_path=rr_path)
        return out.getvalue()
    finally:
        iso.close()


def test_prepare_iso_adds_serial_console_to_automated_entry(tmp_path: Path) -> None:
    from testrange.builders._proxmox_prepare import prepare_iso

    src = tmp_path / "source.iso"
    out = tmp_path / "prepared.iso"
    _make_source_iso_with_grub(src)

    prepare_iso(
        src,
        out,
        partition_label="PROXMOX-AIS",
        first_boot_script="#!/bin/bash\necho first-boot\n",
    )

    grub = _read_iso_file(out, "/boot/grub/grub.cfg").decode("utf-8")
    auto = next(
        ln for ln in grub.splitlines()
        if "proxmox-start-auto-installer" in ln and ln.lstrip().startswith("linux")
    )
    assert "console=ttyS0,115200" in auto, f"serial console not grafted: {auto!r}"
    assert "splash=silent" not in auto and "quiet" not in auto, f"still silent: {auto!r}"


def test_prepare_iso_injects_activation_and_first_boot(tmp_path: Path) -> None:
    from testrange.builders._proxmox_prepare import prepare_iso

    src = tmp_path / "source.iso"
    out = tmp_path / "prepared.iso"
    _make_source_iso(src)

    prepare_iso(
        src,
        out,
        partition_label="PROXMOX-AIS",
        first_boot_script="#!/bin/bash\necho first-boot\n",
    )

    assert out.exists()
    # The original payload survived and the two new files were appended.
    names = _iso_filenames(out)
    assert "hello.txt" in names, f"source content lost: {names}"
    assert "auto-installer-mode.toml" in names, f"activation file missing: {names}"
    assert "proxmox-first-boot" in names, f"first-boot script missing: {names}"


def test_missing_xorriso_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from testrange.builders._proxmox_prepare import ProxmoxPrepareError, prepare_iso

    src = tmp_path / "source.iso"
    _make_source_iso(src)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ProxmoxPrepareError, match="xorriso not found"):
        prepare_iso(
            src,
            tmp_path / "out.iso",
            partition_label="PROXMOX-AIS",
            first_boot_script="#!/bin/bash\n",
        )


def test_nonzero_xorriso_exit_wrapped(tmp_path: Path) -> None:
    # A valid source whose -commit cannot write (output parent missing) drives a
    # genuine non-zero xorriso exit; the wrapper surfaces it loud.
    from testrange.builders._proxmox_prepare import ProxmoxPrepareError, prepare_iso

    src = tmp_path / "source.iso"
    _make_source_iso(src)
    bad_out = tmp_path / "nonexistent-dir" / "out.iso"
    with pytest.raises(ProxmoxPrepareError, match="failed"):
        prepare_iso(
            src,
            bad_out,
            partition_label="PROXMOX-AIS",
            first_boot_script="#!/bin/bash\n",
        )
