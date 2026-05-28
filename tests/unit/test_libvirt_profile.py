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
        prof = load_profile(_write(tmp_path, 'driver = "libvirt"\n'))
        assert isinstance(prof, LibvirtProfile)
        assert prof.uri == "qemu:///system"
        assert prof.backing_pool == "default"

    def test_explicit(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
                tmp_path,
                'driver = "libvirt"\nuri = "qemu:///session"\nbacking_pool = "lab"\n',
            )
        )
        assert isinstance(prof, LibvirtProfile)
        assert prof.uri == "qemu:///session"
        assert prof.backing_pool == "lab"

    def test_unknown_key_named(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown key\(s\) \['nost'\]"):
            load_profile(_write(tmp_path, 'driver = "libvirt"\nnost = "typo"\n'))


class TestBuildDriver:
    def test_builds_libvirt_driver_with_conn(self) -> None:
        drv = LibvirtProfile(uri="qemu:///session", backing_pool="lab").build_driver()
        assert isinstance(drv, LibvirtDriver)
        # Round-trip the teardown URI to confirm both knobs land on the
        # driver-side LibvirtConn (drv.uri itself is the wrapped teardown form
        # with the connect-URI url-quoted inside).
        from testrange.drivers.libvirt._conn import LibvirtConn

        round_tripped = LibvirtConn.from_uri(drv.uri)
        assert round_tripped.libvirt_uri == "qemu:///session"
        assert round_tripped.backing_pool == "lab"


class TestDescribeFields:
    def test_yields_uri_and_backing_pool(self) -> None:
        assert list(LibvirtProfile().describe_fields()) == [
            ("uri", "qemu:///system"),
            ("backing_pool", "default"),
        ]
