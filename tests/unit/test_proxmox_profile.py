"""Tests for :class:`ProxmoxProfile` — the ``driver = "proxmox"`` concrete profile.

Includes the realm-normalization + SSH-defaulting contract: a profile-supplied
connection must resolve identically to the in-Plan ``ProxmoxHypervisor.conn``
path (CORE-18), so a portable plan running under ``--connect`` and a pinned
plan agree on what counts as the same backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.connect import load_profile
from testrange.drivers.proxmox import ProxmoxProfile
from testrange.drivers.proxmox.driver import ProxmoxDriver
from testrange.exceptions import ProfileError
from testrange.networks.base import ManagedBuildSwitch


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "connect.toml"
    p.write_text(text)
    return p


class TestParse:
    def test_full(self, tmp_path: Path) -> None:
        prof = load_profile(
            _write(
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
        )
        assert isinstance(prof, ProxmoxProfile)
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

    def test_minimal(self, tmp_path: Path) -> None:
        prof = load_profile(_write(tmp_path, 'driver = "proxmox"\nhost = "h"\n'))
        assert isinstance(prof, ProxmoxProfile)
        assert prof.host == "h"
        assert prof.user == "root@pam"
        assert prof.password == ""
        assert prof.ssh_user is None
        assert prof.ssh_password is None
        assert prof.build_switch is None

    def test_unknown_key_named(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match=r"unknown key\(s\) \['nost'\]"):
            load_profile(_write(tmp_path, 'driver = "proxmox"\nnost = "typo"\n'))


class TestBuildDriver:
    def test_normalises_bare_user_to_pam(self) -> None:
        # A bare ``user`` takes the @pam realm — the same defaulting as
        # ProxmoxHypervisor.conn. SSH user defaults to the API user's local part;
        # SSH password defaults to the API password.
        drv = ProxmoxProfile(host="10.0.0.5", user="root", password="pw").build_driver()
        assert isinstance(drv, ProxmoxDriver)
        conn = drv._conn  # internal: assert resolved connection without a live PVE
        assert conn.user == "root@pam"
        assert conn.ssh_user == "root"
        assert conn.ssh_password == "pw"
        assert conn.host == "10.0.0.5"

    def test_explicit_realm_preserved(self) -> None:
        drv = ProxmoxProfile(host="h", user="ops@pve", password="pw").build_driver()
        assert drv._conn.user == "ops@pve"
        # SSH derives from the local part of the API user.
        assert drv._conn.ssh_user == "ops"

    def test_explicit_ssh_overrides_defaults(self) -> None:
        drv = ProxmoxProfile(
            host="h",
            user="root",
            password="apipw",
            ssh_user="builder",
            ssh_password="sshpw",
            ssh_port=2222,
        ).build_driver()
        conn = drv._conn
        assert conn.ssh_user == "builder"
        assert conn.ssh_password == "sshpw"
        assert conn.ssh_port == 2222


class TestDescribeFields:
    def test_masks_password(self) -> None:
        fields = list(
            ProxmoxProfile(host="h", user="root@pam", password="secret").describe_fields()
        )
        # Password is masked; the raw value never appears.
        assert ("password", "***set***") in fields
        assert all("secret" not in v for _, v in fields)

    def test_unset_password_renders_unset(self) -> None:
        fields = dict(ProxmoxProfile(host="h").describe_fields())
        assert fields["password"] == "(unset)"

    def test_omits_blank_node(self) -> None:
        labels = [label for label, _ in ProxmoxProfile(host="h").describe_fields()]
        assert "node" not in labels  # auto-detect default, not worth printing

    def test_includes_set_node(self) -> None:
        fields = dict(ProxmoxProfile(host="h", node="pve1").describe_fields())
        assert fields["node"] == "pve1"
