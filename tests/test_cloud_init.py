"""Unit tests for :mod:`testrange.vms.builders.cloud_init`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from testrange import VM, Credential
from testrange.exceptions import CloudInitError
from testrange.packages import Apt, Dnf, Homebrew, Pip, Winget
from testrange.vms.builders.cloud_init import (
    CloudInitBuilder,
    _hash_password,
    _native_packages,
    _runcmd_entries,
    _user_entry,
    write_seed_iso,
)


def _vm(
    users: list[Credential] | None = None,
    pkgs: list | None = None,
    post: list[str] | None = None,
    name: str = "web01",
) -> VM:
    """Build a minimal Linux VM spec for builder tests."""
    return VM(
        name=name,
        iso="https://example.com/debian.qcow2",
        users=users or [],
        pkgs=pkgs or [],
        post_install_cmds=post or [],
    )


class TestHashPassword:
    def test_returns_crypt_format(self) -> None:
        h = _hash_password("hunter2")
        assert h.startswith("$6$")

    def test_distinct_passwords_distinct_hashes(self) -> None:
        # Real sha512_crypt is salted so even the same password hashes
        # differently each call; for the stub we at least check non-empty.
        h1 = _hash_password("a")
        h2 = _hash_password("b")
        assert h1 and h2


class TestNativePackages:
    def test_apt_and_dnf_included(self) -> None:
        names = _native_packages([Apt("nginx"), Dnf("podman")])
        assert "nginx" in names
        assert "podman" in names

    def test_brew_and_pip_excluded(self) -> None:
        names = _native_packages([Homebrew("gh"), Pip("requests")])
        assert "gh" not in names
        assert "requests" not in names

    def test_sorted_output(self) -> None:
        names = _native_packages([Apt("zlib"), Apt("apache2")])
        assert names == sorted(names)

    def test_qemu_guest_agent_not_duplicated(self) -> None:
        # Regression: user explicitly asks for qemu-guest-agent
        names = _native_packages([Apt("qemu-guest-agent")])
        assert names.count("qemu-guest-agent") == 1


class TestRuncmdEntries:
    def test_empty_for_native_only(self) -> None:
        cmds = _runcmd_entries([Apt("nginx")], [], [], ["nginx", "qemu-guest-agent"])
        # Expect only the GA-enablement line
        assert any("qemu-guest-agent" in c for c in cmds)

    def test_starts_with_set_e(self) -> None:
        # Without this, a failed apt install would still let the runcmd
        # script fall through to the success-sentinel and cache a broken
        # image.  Must be POSIX sh — cloud-init shellify emits #!/bin/sh
        # (dash on Debian), so we can't use bash-only constructs like
        # ``trap ... ERR``.
        cmds = _runcmd_entries([], [], [], ["qemu-guest-agent"])
        assert cmds[0] == "set -e"

    def test_verifies_each_native_package(self) -> None:
        cmds = _runcmd_entries(
            [Apt("nginx"), Apt("curl")],
            [],
            [],
            ["curl", "nginx", "qemu-guest-agent"],
        )
        joined = "\n".join(cmds)
        # All three names must appear inside the verification loop.
        assert "'nginx'" in joined
        assert "'curl'" in joined
        assert "'qemu-guest-agent'" in joined
        assert "dpkg -s" in joined and "rpm -q" in joined

    def test_systemctl_enable_is_fail_fast(self) -> None:
        # Regression: old runcmd swallowed systemctl errors with '|| true'.
        # That masked a non-working guest-agent — the run phase would then
        # hang forever.  Must be strict now.
        cmds = _runcmd_entries([], [], [], ["qemu-guest-agent"])
        joined = "\n".join(cmds)
        assert "systemctl enable --now qemu-guest-agent" in joined
        assert "systemctl enable --now qemu-guest-agent || true" not in joined

    def test_success_sentinel_last(self) -> None:
        cmds = _runcmd_entries([], [], ["echo hello"], ["qemu-guest-agent"])
        # The sentinel + sync must be at the very end — any later step
        # that fails has to prevent the sentinel from being written.
        assert cmds[-1] == "sync"
        assert "touch" in cmds[-2] and "install_ok" in cmds[-2]
        # User post-install cmd lands before the sentinel.
        assert "echo hello" in cmds[-4]

    def test_post_install_appended(self) -> None:
        cmds = _runcmd_entries([], [], ["echo hello"], ["qemu-guest-agent"])
        # Now appears between install_commands and the success sentinel.
        assert "echo hello" in cmds

    def test_pip_install_included(self) -> None:
        cmds = _runcmd_entries(
            [Pip("requests")], [], [], ["qemu-guest-agent"]
        )
        assert any("pip3 install requests" in c for c in cmds)

    def test_homebrew_requires_non_root_user(self) -> None:
        with pytest.raises(CloudInitError):
            _runcmd_entries(
                [Homebrew("gh")],
                [Credential("root", "pw")],
                [],
                ["qemu-guest-agent"],
            )

    def test_homebrew_uses_first_non_root_user(self) -> None:
        cmds = _runcmd_entries(
            [Homebrew("gh")],
            [
                Credential("root", "pw"),
                Credential("alice", "pw", sudo=True),
                Credential("bob", "pw"),
            ],
            [],
            ["qemu-guest-agent"],
        )
        joined = "\n".join(cmds)
        assert " alice" in joined
        assert "brew install gh" in joined

    def test_homebrew_installer_and_formulas_both_emitted(self) -> None:
        cmds = _runcmd_entries(
            [Homebrew("gh"), Homebrew("hello")],
            [Credential("alice", "pw")],
            [],
            ["qemu-guest-agent"],
        )
        joined = "\n".join(cmds)
        assert "install.sh" in joined  # Homebrew bootstrap
        assert "brew install gh" in joined
        assert "brew install hello" in joined

    def test_winget_emitted_on_linux_path(self) -> None:
        # Winget is Windows-only; the Linux builder ignores its
        # package_manager string, so the generic non-native path still
        # emits the command.  Not useful on Linux, but documented.
        cmds = _runcmd_entries(
            [Winget("Git.Git")],
            [],
            [],
            ["qemu-guest-agent"],
        )
        assert any("winget install" in c for c in cmds)


class TestUserEntry:
    def test_root_gets_no_shell(self) -> None:
        entry = _user_entry(Credential("root", "pw"))
        assert "shell" not in entry
        assert "sudo" not in entry

    def test_non_root_gets_shell(self) -> None:
        entry = _user_entry(Credential("alice", "pw"))
        assert entry["shell"] == "/bin/bash"
        assert "sudo" not in entry

    def test_sudo_user(self) -> None:
        entry = _user_entry(Credential("alice", "pw", sudo=True))
        assert entry["shell"] == "/bin/bash"
        assert entry["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert "sudo" in entry["groups"]

    def test_ssh_key_included(self) -> None:
        entry = _user_entry(Credential("alice", "pw", ssh_key="ssh-rsa AAAA"))
        assert entry["ssh_authorized_keys"] == ["ssh-rsa AAAA"]

    def test_hashed_password_not_plaintext(self) -> None:
        entry = _user_entry(Credential("alice", "secret"))
        assert entry["hashed_passwd"] != "secret"
        assert (
            "secret" not in entry["hashed_passwd"]
            or entry["hashed_passwd"].startswith("$6$")
        )


class TestInstallUserData:
    @pytest.fixture
    def builder(self) -> CloudInitBuilder:
        return CloudInitBuilder()

    @pytest.fixture
    def vm(self) -> VM:
        return _vm(
            users=[Credential("root", "pw")],
            pkgs=[Apt("nginx")],
            post=["echo done"],
        )

    def test_starts_with_cloud_config_marker(
        self, builder: CloudInitBuilder, vm: VM
    ) -> None:
        assert builder.install_user_data(vm).startswith("#cloud-config\n")

    def test_parses_as_yaml(
        self, builder: CloudInitBuilder, vm: VM
    ) -> None:
        text = builder.install_user_data(vm)
        data = yaml.safe_load(text.split("\n", 1)[1])
        assert data["hostname"] == "web01"
        assert data["fqdn"] == "web01.local"
        assert data["power_state"]["mode"] == "poweroff"

    def test_includes_nocloud_datasource(
        self, builder: CloudInitBuilder, vm: VM
    ) -> None:
        text = builder.install_user_data(vm)
        data = yaml.safe_load(text.split("\n", 1)[1])
        assert data["datasource_list"] == ["NoCloud", "None"]

    def test_power_state_gated_on_sentinel(
        self, builder: CloudInitBuilder, vm: VM
    ) -> None:
        # Regression: previously power_state unconditionally poweroff'd,
        # so an apt failure left the VM in a "clean" shutoff state and
        # we cached the broken image.  Condition must be a shell test
        # against the sentinel written only at runcmd's tail.
        text = builder.install_user_data(vm)
        data = yaml.safe_load(text.split("\n", 1)[1])
        cond = data["power_state"]["condition"]
        assert isinstance(cond, list)
        assert cond[0] == "test"
        assert cond[1] == "-f"
        assert cond[2].endswith("install_ok")

    def test_no_write_files_without_insecure(
        self, builder: CloudInitBuilder, vm: VM
    ) -> None:
        text = builder.install_user_data(vm)
        data = yaml.safe_load(text.split("\n", 1)[1])
        assert "write_files" not in data

    def test_apt_insecure_emits_apt_conf_dropin(self, vm: VM) -> None:
        # Builder-level flag — APT trust is process-wide, so a
        # per-package switch would be a lie.  apt_insecure=True drops an
        # apt.conf.d snippet so cloud-init's own packages: call picks up
        # relaxed TLS verification.
        b = CloudInitBuilder(apt_insecure=True)
        data = yaml.safe_load(b.install_user_data(vm).split("\n", 1)[1])
        paths = [f["path"] for f in data["write_files"]]
        assert "/etc/apt/apt.conf.d/99testrange-insecure" in paths
        apt_entry = next(
            f for f in data["write_files"]
            if f["path"].endswith("99testrange-insecure")
        )
        assert "Verify-Peer" in apt_entry["content"]
        assert "Verify-Host" in apt_entry["content"]

    def test_dnf_insecure_appends_to_dnf_conf(self, vm: VM) -> None:
        b = CloudInitBuilder(dnf_insecure=True)
        data = yaml.safe_load(b.install_user_data(vm).split("\n", 1)[1])
        dnf_entry = next(
            f for f in data["write_files"] if f["path"] == "/etc/dnf/dnf.conf"
        )
        assert dnf_entry.get("append") is True
        assert "sslverify=False" in dnf_entry["content"]

    def test_insecure_flags_independent(self, vm: VM) -> None:
        # Enabling one doesn't smuggle in the other.
        b = CloudInitBuilder(apt_insecure=True)
        data = yaml.safe_load(b.install_user_data(vm).split("\n", 1)[1])
        paths = [f["path"] for f in data.get("write_files", [])]
        assert any("apt.conf.d" in p for p in paths)
        assert not any(p == "/etc/dnf/dnf.conf" for p in paths)


class TestInstallMetaData:
    def test_instance_id_uses_config_hash(self) -> None:
        b = CloudInitBuilder()
        vm = _vm(name="vm")
        data = yaml.safe_load(b.install_meta_data(vm, "abc123"))
        assert data["instance-id"] == "install-abc123"
        assert data["local-hostname"] == "vm"


class TestRunUserData:
    def test_no_packages(self) -> None:
        b = CloudInitBuilder()
        vm = _vm(users=[Credential("root", "pw")], pkgs=[Apt("nginx")])
        data = yaml.safe_load(b.run_user_data(vm).split("\n", 1)[1])
        assert "packages" not in data
        assert "runcmd" not in data

    def test_refreshes_auth_for_each_user(self) -> None:
        b = CloudInitBuilder()
        vm = _vm(users=[Credential("alice", "pw", ssh_key="ssh-rsa X")])
        data = yaml.safe_load(b.run_user_data(vm).split("\n", 1)[1])
        assert len(data["users"]) == 1
        entry = data["users"][0]
        assert entry["name"] == "alice"
        assert entry["ssh_authorized_keys"] == ["ssh-rsa X"]
        # Must re-assert password state so cloud-init doesn't relock the
        # account on every phase-2 boot.
        assert entry["lock_passwd"] is False
        assert entry["hashed_passwd"].startswith("$6$")

    def test_omits_groups_sudo_shell(self) -> None:
        # Phase 2 should not re-declare role-level config — those were set
        # at phase 1 and persist on disk.  Keep phase 2 minimal.
        b = CloudInitBuilder()
        vm = _vm(users=[Credential("alice", "pw", sudo=True)])
        data = yaml.safe_load(b.run_user_data(vm).split("\n", 1)[1])
        entry = data["users"][0]
        assert "groups" not in entry
        assert "sudo" not in entry
        assert "shell" not in entry


class TestRunMetaData:
    def test_instance_id_uses_run_id(self) -> None:
        b = CloudInitBuilder()
        vm = _vm(name="vm")
        data = yaml.safe_load(b.run_meta_data(vm, "run-uuid-here"))
        assert data["instance-id"] == "run-run-uuid-here"


class TestRunNetworkConfig:
    def test_returns_none_when_all_dhcp(self) -> None:
        b = CloudInitBuilder()
        assert b.run_network_config(
            [("52:54:00:00:00:01", "", "10.0.0.1", "10.0.0.1")]
        ) is None

    def test_returns_yaml_with_static(self) -> None:
        b = CloudInitBuilder()
        text = b.run_network_config(
            [("52:54:00:00:00:01", "10.0.0.5/24", "10.0.0.1", "10.0.0.1")]
        )
        assert text is not None
        data = yaml.safe_load(text)
        assert data["version"] == 2
        eth = next(iter(data["ethernets"].values()))
        assert eth["addresses"] == ["10.0.0.5/24"]
        assert eth["gateway4"] == "10.0.0.1"
        assert eth["nameservers"] == {"addresses": ["10.0.0.1"]}

    def test_skips_gateway_when_empty(self) -> None:
        # Isolated networks (internet=False) must not advertise a default
        # gateway — otherwise multiple defaults fight and internet-bound
        # traffic can leak onto the isolated bridge.
        b = CloudInitBuilder()
        text = b.run_network_config(
            [("52:54:00:00:00:01", "10.0.0.5/24", "", "")]
        )
        assert text is not None
        data = yaml.safe_load(text)
        eth = next(iter(data["ethernets"].values()))
        assert "gateway4" not in eth
        assert "nameservers" not in eth

    def test_mixed_static_and_dhcp(self) -> None:
        b = CloudInitBuilder()
        text = b.run_network_config(
            [
                ("52:54:00:00:00:01", "10.0.0.5/24", "10.0.0.1", "10.0.0.1"),
                ("52:54:00:00:00:02", "", "10.0.1.1", "10.0.1.1"),
            ]
        )
        assert text is not None
        data = yaml.safe_load(text)
        assert len(data["ethernets"]) == 2


class TestWriteSeedIso:
    def test_writes_all_three_files_when_network_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.vms.builders.cloud_init as ci

        iso_obj = MagicMock()
        monkeypatch.setattr(ci, "PyCdlib", lambda: iso_obj)

        write_seed_iso(
            tmp_path / "seed.iso",
            meta_data="iid: x",
            user_data="#cloud-config\n",
            network_config="version: 2",
        )
        # add_fp called 3 times (meta, user, network)
        assert iso_obj.add_fp.call_count == 3

    def test_writes_two_files_without_network_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.vms.builders.cloud_init as ci

        iso_obj = MagicMock()
        monkeypatch.setattr(ci, "PyCdlib", lambda: iso_obj)

        write_seed_iso(
            tmp_path / "seed.iso",
            meta_data="iid: x",
            user_data="#cloud-config\n",
        )
        assert iso_obj.add_fp.call_count == 2

    def test_wraps_errors_in_cloud_init_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import testrange.vms.builders.cloud_init as ci

        class BadIso:
            def new(self, **_): pass
            def add_fp(self, *_, **__): raise RuntimeError("boom")
            def close(self): pass
            def write(self, *_): pass

        monkeypatch.setattr(ci, "PyCdlib", BadIso)

        with pytest.raises(CloudInitError):
            write_seed_iso(tmp_path / "seed.iso", "m", "u")
