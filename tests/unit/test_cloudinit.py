"""Tests for CloudInitBuilder rendering + config_hash + seed ISO bytes."""

from __future__ import annotations

import io

import pytest
import yaml

from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.packages import Apt, Pip
from testrange.vms import VMRecipe, VMSpec


def _spec(name: str = "web") -> VMSpec:
    return VMSpec(
        name=name,
        devices=[CPU(1), Memory(512), OSDrive("p1", 8), LibvirtNetworkIface("netA")],
    )


def _recipe(builder: CloudInitBuilder, spec: VMSpec | None = None) -> VMRecipe:
    return VMRecipe(
        spec=spec or _spec(),
        builder=builder,
        communicator=SSHCommunicator("u"),
    )


def _basic_builder() -> CloudInitBuilder:
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[
            PosixCred("root", password="rootpass"),
            PosixCred("u", password="upass", pubkey="ssh-ed25519 AAA... u", sudo=True),
        ],
        packages=[Apt("nginx"), Apt("curl"), Pip("requests")],
        post_install_commands=("echo hi > /tmp/hi",),
    )


class TestRenderUserData:
    def test_starts_with_cloud_config_header(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        text = b.render_user_data(spec, recipe)
        assert text.startswith("#cloud-config\n")

    def test_yaml_is_valid(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        text = b.render_user_data(spec, recipe)
        body = yaml.safe_load(text)
        assert isinstance(body, dict)

    def test_users_with_pubkey(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        u = next(u for u in body["users"] if u["name"] == "u")
        assert u["ssh_authorized_keys"] == ["ssh-ed25519 AAA... u"]
        assert u["sudo"] == "ALL=(ALL) NOPASSWD:ALL"

    def test_chpasswd(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        cp = body["chpasswd"]["list"]
        assert "root:rootpass" in cp
        assert "u:upass" in cp

    def test_apt_packages(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        assert body["packages"] == ["nginx", "curl"]
        assert body["package_update"] is True

    def test_runcmd_ends_with_poweroff(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        assert body["runcmd"][-1] == "poweroff"
        assert "echo hi > /tmp/hi" in body["runcmd"]

    def test_pip_packages_via_runcmd(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        joined = "\n".join(body["runcmd"])
        assert "pip3 install" in joined
        assert "requests" in joined

    def test_no_credentials_no_chpasswd(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            credentials=(),
        )
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe))
        assert "chpasswd" not in body


class TestInsecureFlags:
    def test_default_no_write_files(self) -> None:
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        assert "write_files" not in body

    def test_insecure_apt_drops_conf_d_file(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("debian-13"),
            packages=[Apt("nginx")],
            insecure_apt=True,
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        assert "/etc/apt/apt.conf.d/99-testrange-insecure" in wf
        content = wf["/etc/apt/apt.conf.d/99-testrange-insecure"]["content"]
        assert "Acquire::AllowInsecureRepositories" in content
        assert "APT::Get::AllowUnauthenticated" in content

    def test_insecure_dnf_appends_to_dnf_conf(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("rocky-9"),
            insecure_dnf=True,
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        assert wf["/etc/dnf/dnf.conf"]["append"] is True
        content = wf["/etc/dnf/dnf.conf"]["content"]
        assert "sslverify=False" in content
        assert "gpgcheck=0" in content

    def test_both_flags_together(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            insecure_apt=True,
            insecure_dnf=True,
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        paths = {entry["path"] for entry in body["write_files"]}
        assert "/etc/apt/apt.conf.d/99-testrange-insecure" in paths
        assert "/etc/dnf/dnf.conf" in paths

    def test_non_bool_raises(self) -> None:
        with pytest.raises(TypeError, match="insecure_apt must be bool"):
            CloudInitBuilder(base=CacheEntry("x"), insecure_apt="yes")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="insecure_dnf must be bool"):
            CloudInitBuilder(base=CacheEntry("x"), insecure_dnf=1)  # type: ignore[arg-type]


class TestPipInsecure:
    def test_default_secure_pip_one_install_line(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            packages=[Pip("requests"), Pip("rich")],
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        pip_lines = [c for c in body["runcmd"] if "pip3 install" in c]
        assert len(pip_lines) == 1
        assert "--trusted-host" not in pip_lines[0]
        assert "requests" in pip_lines[0] and "rich" in pip_lines[0]

    def test_insecure_pip_gets_trusted_host_in_separate_line(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            packages=[
                Pip("requests"),
                Pip("internal-pkg", insecure=True),
            ],
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        pip_lines = [c for c in body["runcmd"] if "pip3 install" in c]
        assert len(pip_lines) == 2
        secure_line = next(line for line in pip_lines if "--trusted-host" not in line)
        insecure_line = next(line for line in pip_lines if "--trusted-host" in line)
        assert "requests" in secure_line
        assert "internal-pkg" in insecure_line
        assert "pypi.org" in insecure_line
        assert "files.pythonhosted.org" in insecure_line
        # Secure packages must NOT leak into the insecure install line.
        assert "requests" not in insecure_line

    def test_insecure_only_no_secure_line(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            packages=[Pip("internal-pkg", insecure=True)],
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        pip_lines = [c for c in body["runcmd"] if "pip3 install" in c]
        assert len(pip_lines) == 1
        assert "--trusted-host" in pip_lines[0]

    def test_insecure_non_bool_raises(self) -> None:
        with pytest.raises(TypeError, match=r"Pip\.insecure must be bool"):
            Pip("x", insecure="yes")  # type: ignore[arg-type]


class TestRenderMetaData:
    def test_has_instance_id_and_hostname(self) -> None:
        b = _basic_builder()
        spec = _spec("web")
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_meta_data(spec, recipe))
        assert body["instance-id"] == "iid-web"
        assert body["local-hostname"] == "web"


class TestRenderNetworkConfig:
    def test_matches_by_name_not_mac(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_network_config(spec, recipe))
        assert body["version"] == 2
        ifaces = body["ethernets"]
        assert ifaces
        for cfg in ifaces.values():
            assert "macaddress" not in cfg.get("match", {})
            assert cfg["dhcp4"] is True


class TestConfigHash:
    def test_deterministic(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        h1 = b.config_hash(spec, recipe, base_sha="abc")
        h2 = b.config_hash(spec, recipe, base_sha="abc")
        assert h1 == h2
        assert len(h1) == 16

    def test_base_sha_affects_hash(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        assert b.config_hash(spec, recipe, base_sha="aaa") != b.config_hash(
            spec, recipe, base_sha="bbb"
        )

    def test_credentials_affect_hash(self) -> None:
        spec = _spec()
        b1 = CloudInitBuilder(
            base=CacheEntry("x"),
            credentials=[PosixCred("u", password="a")],
        )
        b2 = CloudInitBuilder(
            base=CacheEntry("x"),
            credentials=[PosixCred("u", password="b")],
        )
        r1 = _recipe(b1, spec)
        r2 = _recipe(b2, spec)
        assert b1.config_hash(spec, r1, base_sha="z") != b2.config_hash(spec, r2, base_sha="z")


class TestRenderSeed:
    def test_seed_bytes_are_iso(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        data = b.render_seed(spec, recipe)
        # Standard ISO9660 sig at offset 0x8001
        assert data[0x8001:0x8006] == b"CD001"

    def test_seed_contains_user_data(self, tmp_path: pytest.TempPathFactory) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        data = b.render_seed(spec, recipe)
        # Use pycdlib to read back
        import pycdlib

        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(data))
        files: dict[str, bytes] = {}
        for joliet in ("/user-data", "/meta-data", "/network-config"):
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, joliet_path=joliet)
            files[joliet] = buf.getvalue()
        iso.close()
        assert files["/user-data"].startswith(b"#cloud-config\n")
        assert b"instance-id" in files["/meta-data"]
        assert b"version" in files["/network-config"]
