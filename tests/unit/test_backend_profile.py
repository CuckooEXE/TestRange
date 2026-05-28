"""Tests for the connection-profile ABC + dispatch (CORE-9 / CORE-18).

Covers what's backend-agnostic in :mod:`testrange.connect`:

- ``load_profile`` reads TOML and dispatches on ``driver`` to the registered
  concrete subclass (Mock/Libvirt/Proxmox);
- :class:`BackendProfile` can't be instantiated directly (it's an ABC);
- the common ``[build_switch]`` table is parsed identically across backends;
- the common validation errors (missing/empty driver, unknown scheme, malformed
  ``[build_switch]``) raise :class:`ProfileError`.

The per-backend field shape, defaulting (e.g., PVE realm normalization), and
``build_driver()`` behavior are tested alongside the concrete subclasses
themselves (test_proxmox_profile.py, test_libvirt_profile.py,
test_mock_profile.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import BackendProfile, load_profile
from testrange.drivers.libvirt import LibvirtProfile
from testrange.drivers.mock import MockProfile
from testrange.drivers.proxmox import ProxmoxProfile
from testrange.exceptions import ProfileError
from testrange.networks.base import ManagedBuildSwitch


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestDispatch:
    def test_mock_scheme_returns_mock_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "mock"\n'))
        assert isinstance(prof, MockProfile)
        assert prof.scheme == "mock"

    def test_libvirt_scheme_returns_libvirt_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "libvirt"\n'))
        assert isinstance(prof, LibvirtProfile)

    def test_proxmox_scheme_returns_proxmox_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "proxmox"\nhost = "h"\n'))
        assert isinstance(prof, ProxmoxProfile)

    def test_unknown_scheme_lists_known(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown driver scheme 'bogus'") as ei:
            load_profile(_write(tmp_path, 'driver = "bogus"\n'))
        msg = str(ei.value)
        assert "mock" in msg and "proxmox" in msg and "libvirt" in msg


class TestCommonErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not found"):
            load_profile(tmp_path / "nope.toml")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not valid TOML"):
            load_profile(_write(tmp_path, "this is = = not toml"))

    def test_missing_driver(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty 'driver'"):
            load_profile(_write(tmp_path, 'host = "10.0.0.5"\n'))

    def test_empty_driver(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty 'driver'"):
            load_profile(_write(tmp_path, 'driver = ""\n'))


class TestBuildSwitchCommon:
    """``[build_switch]`` is the one keyset every backend understands; the parse
    lives on the ABC so its shape errors are identical across schemes."""

    def test_uplink_only(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "mock"\n[build_switch]\nuplink = "vmbr9"\n'))
        assert prof.build_switch == ManagedBuildSwitch(uplink="vmbr9")

    def test_uplink_plus_cidr(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
                tmp_path,
                'driver = "mock"\n[build_switch]\nuplink = "vmbr9"\ncidr = "10.10.10.0/24"\n',
            )
        )
        assert prof.build_switch == ManagedBuildSwitch(uplink="vmbr9", cidr="10.10.10.0/24")

    def test_missing_uplink(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="requires a non-empty 'uplink'"):
            load_profile(
                _write(tmp_path, 'driver = "mock"\n[build_switch]\ncidr = "10.10.10.0/24"\n')
            )

    def test_bad_cidr(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"invalid \[build_switch\]"):
            load_profile(
                _write(tmp_path, 'driver = "mock"\n[build_switch]\nuplink = "v"\ncidr = "nope"\n')
            )

    def test_unknown_key(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"\[build_switch\] has unknown key"):
            load_profile(
                _write(tmp_path, 'driver = "mock"\n[build_switch]\nuplink = "v"\nnat = true\n')
            )


class TestABCConstraints:
    def test_cannot_instantiate_abc(self) -> None:
        # BackendProfile is abstract; concrete subclasses are the only construction
        # path. mypy would refuse this too; we belt-and-suspenders at runtime.
        with pytest.raises(TypeError, match="abstract"):
            BackendProfile()  # type: ignore[abstract]
