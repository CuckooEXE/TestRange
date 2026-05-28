"""Tests for the connection-profile loader (CORE-9).

Covers the TOML round-trip, the descoped (inline) secrets policy, the
``[build_switch]`` -> ManagedBuildSwitch mapping, and the validation errors
(missing driver, unknown key, malformed build_switch).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import BackendProfile, load_profile
from testrange.exceptions import ProfileError
from testrange.networks.base import ManagedBuildSwitch


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestLoadProfile:
    def test_full_profile_round_trips(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            """
            driver = "proxmox"
            host = "10.0.0.5"
            user = "root@pam"
            password = "Target123!"
            port = 8006
            verify_ssl = false
            node = "pve1"
            backing_storage = "local"
            ssh_user = "root"
            ssh_password = "sshpw"
            ssh_port = 2222

            [build_switch]
            uplink = "vmbr9"
            cidr = "10.10.10.0/24"
            """,
        )
        prof = load_profile(p)
        assert prof.driver == "proxmox"
        assert prof.host == "10.0.0.5"
        assert prof.user == "root@pam"
        assert prof.password == "Target123!"
        assert prof.port == 8006
        assert prof.verify_ssl is False
        assert prof.node == "pve1"
        assert prof.backing_storage == "local"
        assert prof.ssh_user == "root"
        assert prof.ssh_password == "sshpw"
        assert prof.ssh_port == 2222
        assert prof.build_switch == ManagedBuildSwitch(uplink="vmbr9", cidr="10.10.10.0/24")

    def test_minimal_profile_parses(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "proxmox"\nhost = "h"\n'))
        assert prof.driver == "proxmox"
        assert prof.host == "h"
        assert prof.password == ""
        assert prof.build_switch is None

    def test_driver_only_parses(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "mock"\n'))
        assert prof.driver == "mock"
        assert prof.host is None

    def test_build_switch_uplink_only(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(tmp_path, 'driver = "proxmox"\nhost = "h"\n[build_switch]\nuplink = "vmbr9"\n')
        )
        assert prof.build_switch == ManagedBuildSwitch(uplink="vmbr9")

    # -- error paths --------------------------------------------------------

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

    def test_unknown_key_named(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown key\(s\) \['nost'\]"):
            load_profile(_write(tmp_path, 'driver = "proxmox"\nnost = "typo"\n'))

    def test_build_switch_missing_uplink(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="requires a non-empty 'uplink'"):
            load_profile(
                _write(tmp_path, 'driver = "proxmox"\n[build_switch]\ncidr = "10.10.10.0/24"\n')
            )

    def test_build_switch_bad_cidr(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="invalid \\[build_switch\\]"):
            load_profile(
                _write(
                    tmp_path, 'driver = "proxmox"\n[build_switch]\nuplink = "v"\ncidr = "nope"\n'
                )
            )

    def test_build_switch_unknown_key(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"\[build_switch\] has unknown key"):
            load_profile(
                _write(tmp_path, 'driver = "proxmox"\n[build_switch]\nuplink = "v"\nnat = true\n')
            )


class TestToMapping:
    def test_omits_unset_and_keeps_driver_password(self) -> None:
        prof = BackendProfile(driver="proxmox", host="h")
        mapping = prof.to_mapping()
        assert mapping == {"driver": "proxmox", "password": "", "host": "h"}

    def test_includes_set_connection_fields(self) -> None:
        prof = BackendProfile(
            driver="proxmox", host="h", user="root", port=8006, ssh_port=22, password="pw"
        )
        mapping = prof.to_mapping()
        assert mapping["user"] == "root"
        assert mapping["port"] == 8006
        assert mapping["ssh_port"] == 22
        assert mapping["password"] == "pw"

    def test_excludes_build_switch(self) -> None:
        prof = BackendProfile(driver="proxmox", build_switch=ManagedBuildSwitch(uplink="vmbr9"))
        assert "build_switch" not in prof.to_mapping()

    def test_mapping_feeds_driver_for_profile(self) -> None:
        # The mapping is exactly what the registry consumes (CORE-8 seam).
        from testrange.drivers import driver_for_profile
        from testrange.drivers.proxmox.driver import ProxmoxDriver

        prof = BackendProfile(driver="proxmox", host="10.0.0.5", user="root", password="pw")
        drv = driver_for_profile(prof.to_mapping())
        assert isinstance(drv, ProxmoxDriver)
        assert drv._conn.user == "root@pam"
