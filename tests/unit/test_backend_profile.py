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
from testrange.drivers.proxmox import ProxmoxProfile
from testrange.exceptions import ProfileError
from tests.mock_driver import MockProfile


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
        with pytest.raises(ProfileError, match="host-interface string or a table"):
            load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n[p.uplinks]\nlab = 5\n'), "p")

    def test_empty_string_value_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="non-empty host-interface string"):
            load_profile(_write(tmp_path, '[p]\ndriver = "mock"\n[p.uplinks]\nlab = ""\n'), "p")


class TestUplinkTableForm:
    """NET-8: an uplink value may be a table carrying static sidecar addressing."""

    def _load(self, tmp_path: Path, uplinks_toml: str) -> BackendProfile:
        return load_profile(_write(tmp_path, f'[p]\ndriver = "mock"\n{uplinks_toml}'), "p")

    def test_table_form_parses_bridge_and_addr(self, tmp_path: Path) -> None:
        prof = self._load(
            tmp_path,
            "[p.uplinks.egress]\n"
            'bridge = "vmbr9"\n'
            'sidecar_addr = "10.10.10.2/24"\n'
            'gateway = "10.10.10.1"\n'
            'dns = ["1.1.1.1", "8.8.8.8"]\n',
        )
        assert dict(prof.uplinks) == {"egress": "vmbr9"}  # driver still gets the bridge
        addr = prof.uplink_addrs["egress"]
        assert addr.addr == "10.10.10.2/24"
        assert addr.gw == "10.10.10.1"
        assert addr.dns == ("1.1.1.1", "8.8.8.8")

    def test_string_and_table_forms_coexist(self, tmp_path: Path) -> None:
        prof = self._load(
            tmp_path,
            '[p.uplinks]\nplain = "vmbr3"\n'
            '[p.uplinks.egress]\nbridge = "vmbr9"\nsidecar_addr = "10.10.10.2/24"\n',
        )
        assert dict(prof.uplinks) == {"plain": "vmbr3", "egress": "vmbr9"}
        assert set(prof.uplink_addrs) == {"egress"}  # only the table form carries an addr

    def test_table_form_bridge_only_has_no_addr(self, tmp_path: Path) -> None:
        prof = self._load(tmp_path, '[p.uplinks.egress]\nbridge = "vmbr9"\n')
        assert dict(prof.uplinks) == {"egress": "vmbr9"}
        assert dict(prof.uplink_addrs) == {}

    def test_table_missing_bridge_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="requires a non-empty 'bridge'"):
            self._load(tmp_path, '[p.uplinks.egress]\nsidecar_addr = "10.10.10.2/24"\n')

    def test_table_unknown_key_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="unknown key"):
            self._load(tmp_path, '[p.uplinks.egress]\nbridge = "vmbr9"\nnope = 1\n')

    def test_sidecar_addr_without_prefix_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="explicit prefix"):
            self._load(
                tmp_path, '[p.uplinks.egress]\nbridge = "vmbr9"\nsidecar_addr = "10.10.10.2"\n'
            )

    def test_dns_must_be_list_of_strings(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="dns must be a list of strings"):
            self._load(
                tmp_path,
                '[p.uplinks.egress]\nbridge = "vmbr9"\n'
                'sidecar_addr = "10.10.10.2/24"\ndns = "1.1.1.1"\n',
            )

    def test_gateway_must_be_a_string(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="gateway must be a string"):
            self._load(
                tmp_path,
                '[p.uplinks.egress]\nbridge = "vmbr9"\n'
                'sidecar_addr = "10.10.10.2/24"\ngateway = 42\n',
            )

    def test_invalid_addressing_is_wrapped(self, tmp_path: Path) -> None:
        # A syntactically-OK sidecar_addr whose CIDR StaticAddr rejects must
        # surface as a ProfileError, not a bare ValueError from the device layer.
        with pytest.raises(ProfileError, match="addressing is invalid"):
            self._load(
                tmp_path,
                '[p.uplinks.egress]\nbridge = "vmbr9"\nsidecar_addr = "not-an-ip/24"\n',
            )


class TestABCConstraints:
    def test_cannot_instantiate_abc(self) -> None:
        # BackendProfile is abstract; concrete subclasses are the only construction
        # path. mypy would refuse this too; we belt-and-suspenders at runtime.
        with pytest.raises(TypeError, match="abstract"):
            BackendProfile()  # type: ignore[abstract]
