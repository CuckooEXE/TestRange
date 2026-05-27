"""Tests for CloudInitBuilder rendering + config_hash + seed ISO bytes."""

from __future__ import annotations

import io
from collections.abc import Mapping

import pytest
import yaml

from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.exceptions import BuildNotReadyError
from testrange.guest_io import ExecResult
from testrange.networks import Network, NetworkAddressing, Switch
from testrange.packages import Apt, Pip
from testrange.vms import VMRecipe, VMSpec

# Default per-test addressing map. Builders take a Mapping[network_name,
# NetworkAddressing] from the orchestrator so they never see a hypervisor.
# Both fixture switches have dhcp + dns + nat + uplink set so addr.gateway
# and addr.dns_server are non-None (exercises the full netplan path).
_SW_A = Switch(
    "swA", Network("netA"), cidr="172.31.0.0/24",
    dhcp=True, dns=True, nat=True, uplink="lo",
)
_SW_B = Switch(
    "swB", Network("netB"), cidr="10.10.10.0/24",
    dhcp=True, dns=True, nat=True, uplink="lo",
)
DEFAULT_ADDR: Mapping[str, NetworkAddressing] = {
    "netA": NetworkAddressing.from_switch(_SW_A),
    "netB": NetworkAddressing.from_switch(_SW_B),
}


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


class _FakeExec:
    """A GuestExec-shaped callable that records calls and returns a canned result."""

    def __init__(self, exit_code: int = 0, stderr: bytes = b"") -> None:
        self._exit_code = exit_code
        self._stderr = stderr
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv, *, timeout=60.0, cwd=None):  # type: ignore[no-untyped-def]
        self.calls.append((tuple(argv), timeout))
        return ExecResult(exit_code=self._exit_code, stdout=b"", stderr=self._stderr, duration=0.0)


class TestRenderUserData:
    def test_starts_with_cloud_config_header(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        text = b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR)
        assert text.startswith("#cloud-config\n")

    def test_yaml_is_valid(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        text = b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR)
        body = yaml.safe_load(text)
        assert isinstance(body, dict)

    def test_users_with_pubkey(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
        u = next(u for u in body["users"] if u["name"] == "u")
        assert u["ssh_authorized_keys"] == ["ssh-ed25519 AAA... u"]
        assert u["sudo"] == "ALL=(ALL) NOPASSWD:ALL"

    def test_chpasswd(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
        cp = body["chpasswd"]["list"]
        assert "root:rootpass" in cp
        assert "u:upass" in cp

    def test_apt_packages(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
        assert body["packages"] == ["nginx", "curl"]
        assert body["package_update"] is True

    def test_runcmd_ends_with_poweroff(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
        assert body["runcmd"][-1] == "poweroff"
        assert "echo hi > /tmp/hi" in body["runcmd"]

    def test_pip_packages_via_runcmd(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
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
        body = yaml.safe_load(b.render_user_data(spec, recipe, addressing=DEFAULT_ADDR))
        assert "chpasswd" not in body


class TestInsecureFlags:
    def test_default_no_write_files(self) -> None:
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
        assert "write_files" not in body

    def test_insecure_apt_drops_conf_d_file(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("debian-13"),
            packages=[Apt("nginx")],
            insecure_apt=True,
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
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
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
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
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
        paths = {entry["path"] for entry in body["write_files"]}
        assert "/etc/apt/apt.conf.d/99-testrange-insecure" in paths
        assert "/etc/dnf/dnf.conf" in paths



class TestPipInsecure:
    def test_default_secure_pip_one_install_line(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            packages=[Pip("requests"), Pip("rich")],
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
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
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
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
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
        pip_lines = [c for c in body["runcmd"] if "pip3 install" in c]
        assert len(pip_lines) == 1
        assert "--trusted-host" in pip_lines[0]



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
        body = yaml.safe_load(b.render_network_config(spec, recipe, addressing=DEFAULT_ADDR))
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
        h1 = b.config_hash(spec, recipe, addressing=DEFAULT_ADDR, base_sha="abc")
        h2 = b.config_hash(spec, recipe, addressing=DEFAULT_ADDR, base_sha="abc")
        assert h1 == h2
        assert len(h1) == 16

    def test_base_sha_affects_hash(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        assert b.config_hash(
            spec, recipe, addressing=DEFAULT_ADDR, base_sha="aaa"
        ) != b.config_hash(spec, recipe, addressing=DEFAULT_ADDR, base_sha="bbb")

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
        assert b1.config_hash(spec, r1, addressing=DEFAULT_ADDR, base_sha="z") != b2.config_hash(
            spec, r2, addressing=DEFAULT_ADDR, base_sha="z"
        )


class TestRenderSeed:
    def test_seed_bytes_are_iso(self) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        data = b.render_seed(spec, recipe, addressing=DEFAULT_ADDR)
        # Standard ISO9660 sig at offset 0x8001
        assert data[0x8001:0x8006] == b"CD001"

    def test_seed_contains_user_data(self, tmp_path: pytest.TempPathFactory) -> None:
        b = _basic_builder()
        spec = _spec()
        recipe = _recipe(b, spec)
        data = b.render_seed(spec, recipe, addressing=DEFAULT_ADDR)
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


# ----------------------------------------------------------------------------
# Static-IP / run-phase netplan staging.
# ----------------------------------------------------------------------------


def _static_spec(*nics: LibvirtNetworkIface) -> VMSpec:
    return VMSpec(
        name="web",
        devices=[CPU(1), Memory(512), OSDrive("p1", 8), *nics],
    )


class TestRunPhaseNetplanStaging:
    def test_pure_dhcp_no_extra_write_files(self) -> None:
        # No NIC has ipv4 — install-time DHCP netplan is correct for run-phase too.
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b), addressing=DEFAULT_ADDR))
        paths = {entry["path"] for entry in body.get("write_files", [])}
        assert "/etc/netplan/50-cloud-init.yaml" not in paths
        assert "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg" not in paths

    def test_static_nic_writes_netplan_and_disable_cfg(self) -> None:
        spec = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        np_path = "/etc/netplan/50-cloud-init.yaml"
        dis_path = "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg"
        assert np_path in wf
        assert dis_path in wf

    def test_staged_netplan_has_secure_permissions(self) -> None:
        # netplan 0.106+ warns on world-readable netplan files.
        spec = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        assert wf["/etc/netplan/50-cloud-init.yaml"]["permissions"] == "0600"
        assert wf["/etc/netplan/50-cloud-init.yaml"]["owner"] == "root:root"

    def test_disable_drop_in_content(self) -> None:
        spec = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        content = wf["/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg"]["content"]
        # Parse as YAML to assert semantic content rather than exact bytes.
        cfg = yaml.safe_load(content)
        assert cfg == {"network": {"config": "disabled"}}

    def test_staged_netplan_content_single_static(self) -> None:
        spec = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        netplan = yaml.safe_load(wf["/etc/netplan/50-cloud-init.yaml"]["content"])
        eth = netplan["network"]["ethernets"]["id0"]
        assert eth["addresses"] == ["172.31.0.50/24"]
        assert eth["nameservers"] == {"addresses": ["172.31.0.1"]}
        assert eth["routes"] == [{"to": "default", "via": "172.31.0.1"}]
        assert "dhcp4" not in eth

    def test_staged_netplan_first_static_gets_default_route(self) -> None:
        # Two static NICs: only the first declares the default route.
        spec = _static_spec(
            LibvirtNetworkIface("netA", ipv4="172.31.0.50"),
            LibvirtNetworkIface("netB", ipv4="10.10.10.50"),
        )
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        netplan = yaml.safe_load(wf["/etc/netplan/50-cloud-init.yaml"]["content"])
        eths = netplan["network"]["ethernets"]
        assert "routes" in eths["id0"]
        assert "routes" not in eths["id1"]

    def test_staged_netplan_mixed_static_dhcp(self) -> None:
        # NIC0 static, NIC1 DHCP — netplan reflects both branches.
        spec = _static_spec(
            LibvirtNetworkIface("netA", ipv4="172.31.0.50"),
            LibvirtNetworkIface("netB"),
        )
        b = CloudInitBuilder(base=CacheEntry("x"))
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec), addressing=DEFAULT_ADDR))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        netplan = yaml.safe_load(wf["/etc/netplan/50-cloud-init.yaml"]["content"])
        eths = netplan["network"]["ethernets"]
        assert eths["id0"]["addresses"] == ["172.31.0.50/24"]
        assert eths["id1"]["dhcp4"] is True

    def test_install_network_config_stays_dhcp(self) -> None:
        # The install-time network-config must remain DHCP-only even when a
        # NIC has a static ipv4 — install runs on a different subnet.
        spec = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        b = CloudInitBuilder(base=CacheEntry("x"))
        netcfg = yaml.safe_load(
            b.render_network_config(spec, _recipe(b, spec), addressing=DEFAULT_ADDR)
        )
        eth = netcfg["ethernets"]["id0"]
        assert eth["dhcp4"] is True
        assert "addresses" not in eth

    def test_config_hash_sensitive_to_ipv4(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec_a = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        spec_b = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.60"))
        h_a = b.config_hash(spec_a, _recipe(b, spec_a), addressing=DEFAULT_ADDR, base_sha="z")
        h_b = b.config_hash(spec_b, _recipe(b, spec_b), addressing=DEFAULT_ADDR, base_sha="z")
        assert h_a != h_b

    def test_config_hash_static_vs_dhcp_differs(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec_dhcp = _static_spec(LibvirtNetworkIface("netA"))
        spec_static = _static_spec(LibvirtNetworkIface("netA", ipv4="172.31.0.50"))
        h_dhcp = b.config_hash(
            spec_dhcp, _recipe(b, spec_dhcp), addressing=DEFAULT_ADDR, base_sha="z"
        )
        h_static = b.config_hash(
            spec_static, _recipe(b, spec_static), addressing=DEFAULT_ADDR, base_sha="z"
        )
        assert h_dhcp != h_static


class TestWaitReady:
    def test_cloudinit_runs_status_wait(self) -> None:
        b = _basic_builder()
        spec = _spec()
        ex = _FakeExec()
        b.wait_ready(spec, _recipe(b, spec), ex)
        assert ex.calls == [(("cloud-init", "status", "--wait"), 300.0)]

    def test_cloudinit_raises_on_nonzero(self) -> None:
        b = _basic_builder()
        spec = _spec()
        ex = _FakeExec(exit_code=1, stderr=b"degraded")
        with pytest.raises(BuildNotReadyError, match="exited 1"):
            b.wait_ready(spec, _recipe(b, spec), ex)

    def test_abc_default_is_noop(self) -> None:
        from testrange.builders.base import Builder
        from testrange.credentials.base import Credential

        class _NullBuilder(Builder):
            @property
            def credentials(self) -> tuple[Credential, ...]:
                return ()

            def config_hash(self, spec, recipe, *, addressing, base_sha="", macs=()):  # type: ignore[no-untyped-def]
                return "0" * 16

            def render_seed(self, spec, recipe, *, addressing, macs=()):  # type: ignore[no-untyped-def]
                return b""

        b = _NullBuilder()
        spec = _spec()
        ex = _FakeExec()
        b.wait_ready(spec, _recipe(_basic_builder(), spec), ex)
        assert ex.calls == []  # the ABC default never touches execute
