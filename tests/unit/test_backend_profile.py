"""Tests for the connection-profile ABC + dispatch (CORE-9 / CORE-18 / ADR-0016).

Covers what's backend-agnostic in :mod:`testrange.connect`:

- ``load_profile(path, name)`` reads one TOML file, selects the ``[name]`` table,
  and dispatches on its ``driver`` key to the registered concrete subclass;
- :class:`BackendProfile` can't be instantiated directly (it's an ABC);
- the common ``[<name>.uplinks]`` sub-table is parsed identically across backends;
- the common validation errors (missing profile, missing/empty driver, unknown
  scheme, malformed ``[uplinks]``) raise :class:`ProfileError`.

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


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestDispatch:
    def test_mock_scheme_returns_mock_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n'), "p")
        assert isinstance(prof, MockProfile)
        assert prof.scheme == "mock"

    def test_libvirt_scheme_returns_libvirt_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "libvirt"\n'), "p")
        assert isinstance(prof, LibvirtProfile)

    def test_proxmox_scheme_returns_proxmox_profile(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "proxmox"\nhost = "h"\n'), "p")
        assert isinstance(prof, ProxmoxProfile)

    def test_selects_the_named_profile(self, tmp_path: Path) -> None:
        # One file, many profiles: the name selects which table binds.
        body = '[a]\ndriver = "mock"\n\n[b]\ndriver = "libvirt"\n'
        assert isinstance(load_profile(_write(tmp_path, body), "a"), MockProfile)
        assert isinstance(load_profile(_write(tmp_path, body), "b"), LibvirtProfile)

    def test_unknown_scheme_lists_known(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown driver scheme 'bogus'") as ei:
            load_profile(_write(tmp_path, '[p]\ndriver = "bogus"\n'), "p")
        msg = str(ei.value)
        assert "mock" in msg and "proxmox" in msg and "libvirt" in msg


class TestCommonErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not found"):
            load_profile(tmp_path / "nope.toml", "p")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not valid TOML"):
            load_profile(_write(tmp_path, "this is = = not toml"), "p")

    def test_missing_profile_lists_available(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"no profile named 'nope'") as ei:
            load_profile(_write(tmp_path, '[a]\ndriver = "mock"\n[b]\ndriver = "mock"\n'), "nope")
        msg = str(ei.value)
        assert "'a'" in msg and "'b'" in msg  # lists available profile names

    def test_profile_not_a_table(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="is not a profile table"):
            load_profile(_write(tmp_path, 'p = "scalar"\n'), "p")

    def test_missing_driver(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty 'driver'"):
            load_profile(_write(tmp_path, '[p]\nhost = "10.0.0.5"\n'), "p")

    def test_empty_driver(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty 'driver'"):
            load_profile(_write(tmp_path, '[p]\ndriver = ""\n'), "p")


class TestUplinksCommon:
    """``[<name>.uplinks]`` is the one keyset every backend understands; the parse
    lives on the ABC so its shape errors are identical across schemes."""

    def test_maps_logical_names(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
                tmp_path,
                '[p]\ndriver = "mock"\n[p.uplinks]\nlab = "vmbr3"\negress = "vmbr9"\n',
            ),
            "p",
        )
        assert dict(prof.uplinks) == {"lab": "vmbr3", "egress": "vmbr9"}

    def test_absent_is_empty(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n'), "p")
        assert dict(prof.uplinks) == {}

    def test_non_table_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"\[uplinks\] must be a table"):
            load_profile(_write(tmp_path, '[p]\ndriver = "mock"\nuplinks = "nope"\n'), "p")

    def test_non_string_value_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty host-interface string"):
            load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n[p.uplinks]\nlab = 5\n'), "p")


class TestABCConstraints:
    def test_cannot_instantiate_abc(self) -> None:
        # BackendProfile is abstract; concrete subclasses are the only construction
        # path. mypy would refuse this too; we belt-and-suspenders at runtime.
        with pytest.raises(TypeError, match="abstract"):
            BackendProfile()  # type: ignore[abstract]
