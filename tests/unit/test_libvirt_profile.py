"""Tests for :class:`LibvirtProfile` — the ``driver = "libvirt"`` concrete profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import load_profile
from testrange.drivers.libvirt import LibvirtProfile
from testrange.drivers.libvirt.driver import LibvirtDriver
from testrange.exceptions import ProfileError


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestParse:
    def test_defaults(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "libvirt"\n'), "p")
        assert isinstance(prof, LibvirtProfile)
        assert prof.uri == "qemu:///system"
        assert dict(prof.uplinks) == {}

    def test_explicit(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
                tmp_path,
                '[p]\ndriver = "libvirt"\nuri = "qemu:///session"\n'
                '[p.uplinks]\negress = "tr-egress"\n',
            ),
            "p",
        )
        assert isinstance(prof, LibvirtProfile)
        assert prof.uri == "qemu:///session"
        assert dict(prof.uplinks) == {"egress": "tr-egress"}

    def test_unknown_key_named(self, tmp_path: Path) -> None:
        # backing_pool was removed (BACKEND-1): a stale knob is now an unknown key.
        with pytest.raises(ProfileError, match=r"unknown key\(s\) \['backing_pool'\]"):
            load_profile(
                _write(tmp_path, '[p]\ndriver = "libvirt"\nbacking_pool = "default"\n'), "p"
            )


class TestBuildDriver:
    def test_builds_libvirt_driver_with_conn(self) -> None:
        drv = LibvirtProfile(uri="qemu:///session", uplinks={"egress": "tr-egress"}).build_driver()
        assert isinstance(drv, LibvirtDriver)
        assert drv._uplinks == {"egress": "tr-egress"}
        # Round-trip the teardown URI to confirm the uri lands on the driver-side
        # LibvirtConn (drv.uri itself is the wrapped teardown form with the
        # connect-URI url-quoted inside).
        from testrange.drivers.libvirt._conn import LibvirtConn

        round_tripped = LibvirtConn.from_uri(drv.uri)
        assert round_tripped.libvirt_uri == "qemu:///session"


class TestDescribeFields:
    def test_yields_uri(self) -> None:
        assert list(LibvirtProfile().describe_fields()) == [("uri", "qemu:///system")]
