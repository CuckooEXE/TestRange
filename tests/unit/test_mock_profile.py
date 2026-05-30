"""Tests for :class:`MockProfile` — the ``driver = "mock"`` concrete profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import load_profile
from testrange.exceptions import ProfileError
from tests.mock_driver import MockDriver, MockProfile


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestParse:
    def test_minimal(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n'), "p")
        assert isinstance(prof, MockProfile)
        assert prof.pool_root is None
        assert prof.backing_capacity_gb is None
        assert dict(prof.uplinks) == {}

    def test_full(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
                tmp_path,
                '[p]\ndriver = "mock"\npool_root = "/tmp/p"\nbacking_capacity_gb = 64\n'
                '[p.uplinks]\negress = "br0"\n',
            ),
            "p",
        )
        assert isinstance(prof, MockProfile)
        assert prof.pool_root == Path("/tmp/p")
        assert prof.backing_capacity_gb == 64
        assert dict(prof.uplinks) == {"egress": "br0"}

    def test_unknown_key_named(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown key\(s\) \['nost'\]"):
            load_profile(_write(tmp_path, '[p]\ndriver = "mock"\nnost = "typo"\n'), "p")


class TestBuildDriver:
    def test_default_builds_mock_driver(self) -> None:
        drv = MockProfile().build_driver()
        assert isinstance(drv, MockDriver)
        # Defaulting: pool_root gets a temp dir; capacity stays unlimited.
        assert drv.pool_root.exists()
        assert drv.backing_capacity_gb is None
        assert drv.uplinks == {}

    def test_honours_knobs(self, tmp_path: Path) -> None:
        drv = MockProfile(
            pool_root=tmp_path, backing_capacity_gb=32, uplinks={"egress": "br0"}
        ).build_driver()
        assert isinstance(drv, MockDriver)
        assert drv.pool_root == tmp_path
        assert drv.backing_capacity_gb == 32
        assert drv.uplinks == {"egress": "br0"}


class TestDescribeFields:
    def test_empty_when_defaults(self) -> None:
        assert list(MockProfile().describe_fields()) == []

    def test_yields_set_knobs(self, tmp_path: Path) -> None:
        fields = list(MockProfile(pool_root=tmp_path, backing_capacity_gb=8).describe_fields())
        assert ("pool_root", str(tmp_path)) in fields
        assert ("backing_capacity_gb", "8 GiB") in fields
