"""Tests for CloudInitBuilder rendering + config_hash + seed ISO bytes.

ADR-0017: ``network-config`` is the single, final match-by-MAC netplan — it
carries the dedicated build NIC plus every declared NIC, with no install-vs-run
staging. ``render_user_data`` no longer renders any netplan; the only network
``write_files`` entry left is the unconditional disable-network guard.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

import pytest
import yaml

from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, HardDrive, Memory, OSDrive, StaticAddr
from testrange.devices.network import NetworkIface
from testrange.exceptions import BuildNotReadyError
from testrange.guest_io import ExecResult
from testrange.networks import Network, NetworkAddressing, Sidecar, Switch
from testrange.networks.base import BuildNic
from testrange.packages import Apt, Pip
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="cloudinit-test")

# Default per-test addressing map. Builders take a Mapping[network_name,
# NetworkAddressing] from the orchestrator so they never see a hypervisor.
# Both fixture switches have dhcp + dns + nat + uplink set so addr.gateway
# and addr.dns_server are non-None (exercises the full netplan path).
_SW_A = Switch(
    "swA",
    Network("netA"),
    cidr="172.31.0.0/24",
    uplink="lo",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)
_SW_B = Switch(
    "swB",
    Network("netB"),
    cidr="10.10.10.0/24",
    uplink="lo",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)
DEFAULT_ADDR: Mapping[str, NetworkAddressing] = {
    "netA": NetworkAddressing.from_switch(_SW_A),
    "netB": NetworkAddressing.from_switch(_SW_B),
}

# The build switch the dedicated build NIC lives on (ADR-0017): nat + dns, so the
# build NIC's static .3 derives a gateway/DNS at the sidecar .1 (apt egress).
_BUILD_SW = Switch(
    "build",
    Network("build-net"),
    cidr="10.97.99.0/24",
    uplink="lo",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)


def _build_nic(mac: str = "02:00:00:aa:bb:cc") -> BuildNic:
    """A build NIC at the build switch's .3 infra slot, MAC-matched."""
    return BuildNic(
        mac=mac,
        network="build-net",
        addr=StaticAddr("10.97.99.3"),
        addressing=NetworkAddressing.from_switch(_BUILD_SW),
    )


def _macs(spec: VMSpec) -> tuple[str, ...]:
    """One deterministic declared-NIC MAC per NIC, in spec order."""
    return tuple(f"02:00:00:00:00:{i:02x}" for i in range(len(spec.nics)))


def _spec(name: str = "web") -> VMSpec:
    return VMSpec(
        name=name,
        devices=[
            CPU(1),
            Memory(512),
            OSDrive("p1", 8),
            NetworkIface("netA", addr=DHCPAddr()),
        ],
    )


def _recipe(builder: CloudInitBuilder, spec: VMSpec | None = None) -> VMRecipe:
    return VMRecipe(
        spec=spec or _spec(),
        builder=builder,
        communicator=SSHCommunicator("u"),
    )


def _netcfg(
    b: CloudInitBuilder,
    spec: VMSpec,
    *,
    addressing: Mapping[str, NetworkAddressing] = DEFAULT_ADDR,
    build_nic: BuildNic | None = None,
) -> dict[str, Any]:
    """Render network-config and parse the unwrapped netplan."""
    body: dict[str, Any] = yaml.safe_load(
        b.render_network_config(
            spec,
            _recipe(b, spec),
            addressing=addressing,
            build_nic=build_nic or _build_nic(),
            macs=_macs(spec),
        )
    )
    return body


def _provision_script(body: dict[str, Any]) -> str:
    """Extract the bash provisioning script from the single ``runcmd`` entry.

    All provisioning now runs inside one ``["bash", "-c", <script>]`` entry so
    it executes fail-fast under an ERR trap that emits the build-result record
    (ADR §21).
    """
    runcmd = body["runcmd"]
    assert len(runcmd) == 1
    entry = runcmd[0]
    assert entry[:2] == ["bash", "-c"]
    return str(entry[2])


def _basic_builder() -> CloudInitBuilder:
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[
            PosixCred("root", password="rootpass"),
            PosixCred("u", password="upass", ssh_key=_KEY, admin=True),
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
        text = b.render_user_data(spec, _recipe(b, spec))
        assert text.startswith("#cloud-config\n")

    def test_yaml_is_valid(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        assert isinstance(body, dict)

    def test_users_with_pubkey(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        u = next(u for u in body["users"] if u["name"] == "u")
        assert u["ssh_authorized_keys"] == [_KEY.auth_line]
        assert u["sudo"] == "ALL=(ALL) NOPASSWD:ALL"

    def test_chpasswd(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        # Modern cloud-init form: chpasswd.users[] with type=text, not the
        # deprecated top-level `list:` string.
        assert "list" not in body["chpasswd"]
        users = {u["name"]: u for u in body["chpasswd"]["users"]}
        assert users["root"] == {"name": "root", "type": "text", "password": "rootpass"}
        assert users["u"] == {"name": "u", "type": "text", "password": "upass"}
        assert body["chpasswd"]["expire"] is False

    def test_apt_installed_fail_fast_in_script(self) -> None:
        # apt lives in the trapped script (not cloud-init's `packages:`
        # directive) so a failed install aborts and reports, not silently
        # caches a half-provisioned disk (ADR §21).
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        assert "packages" not in body
        assert "package_update" not in body
        script = _provision_script(body)
        assert "apt-get update" in script
        assert "apt-get install -y nginx curl" in script

    def test_script_is_fail_fast_emits_result_and_powers_off(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        script = _provision_script(body)
        assert "set -eE" in script
        assert "trap __tr_emit_fail ERR" in script
        assert "TESTRANGE-RESULT: ok" in script  # the success token
        assert "TESTRANGE-RESULT: fail" in script  # the trap's failure record
        assert "echo hi > /tmp/hi" in script  # post_install_commands run inline
        assert script.rstrip().endswith("poweroff")  # self-terminating

    def test_pip_packages_via_script(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        script = _provision_script(body)
        assert "pip3 install" in script
        assert "requests" in script

    def test_no_credentials_no_chpasswd(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"), credentials=())
        spec = _spec()
        body = yaml.safe_load(b.render_user_data(spec, _recipe(b, spec)))
        assert "chpasswd" not in body


class TestDisableNetworkGuard:
    def test_default_write_files_pins_netplan(self) -> None:
        # ADR-0017 §4: the disable-network drop-in is unconditional — it pins
        # the build-boot-rendered netplan across the seed-less run boot.
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        dis = "/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg"
        assert dis in wf
        assert yaml.safe_load(wf[dis]["content"]) == {"network": {"config": "disabled"}}

    def test_no_netplan_staged_in_write_files(self) -> None:
        # The /etc/netplan staging file is gone — the netplan is delivered as
        # network-config, not smuggled via write_files.
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        paths = {entry["path"] for entry in body["write_files"]}
        assert "/etc/netplan/50-cloud-init.yaml" not in paths


class TestInsecureFlags:
    def test_default_no_apt_conf_d_file(self) -> None:
        b = _basic_builder()
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        paths = {entry["path"] for entry in body["write_files"]}
        assert "/etc/apt/apt.conf.d/99-testrange-insecure" not in paths

    def test_insecure_pkg_manager_drops_apt_conf_d_file(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("debian-13"),
            packages=[Apt("nginx")],
            insecure_pkg_manager=True,
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        wf = {entry["path"]: entry for entry in body["write_files"]}
        assert "/etc/apt/apt.conf.d/99-testrange-insecure" in wf
        content = wf["/etc/apt/apt.conf.d/99-testrange-insecure"]["content"]
        assert "Acquire::AllowInsecureRepositories" in content
        assert "APT::Get::AllowUnauthenticated" in content

    def test_insecure_pkg_manager_is_apt_only(self) -> None:
        # The single flag emits the apt drop-in only — no dnf config. (The
        # disable-network guard is always present and is not a package-manager
        # file.)
        b = CloudInitBuilder(base=CacheEntry("x"), insecure_pkg_manager=True)
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        apt_paths = {p["path"] for p in body["write_files"] if "apt" in p["path"]}
        assert apt_paths == {"/etc/apt/apt.conf.d/99-testrange-insecure"}


class TestPackageValidation:
    def test_rejects_non_apt_pip_package(self) -> None:
        with pytest.raises(ValueError, match="must be Apt or Pip"):
            CloudInitBuilder(base=CacheEntry("x"), packages=["nginx"])  # type: ignore[list-item]


class TestPipInsecure:
    def test_default_secure_pip_one_install_line(self) -> None:
        b = CloudInitBuilder(
            base=CacheEntry("x"),
            packages=[Pip("requests"), Pip("rich")],
        )
        body = yaml.safe_load(b.render_user_data(_spec(), _recipe(b)))
        pip_lines = [ln for ln in _provision_script(body).splitlines() if "pip3 install" in ln]
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
        pip_lines = [ln for ln in _provision_script(body).splitlines() if "pip3 install" in ln]
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
        pip_lines = [ln for ln in _provision_script(body).splitlines() if "pip3 install" in ln]
        assert len(pip_lines) == 1
        assert "--trusted-host" in pip_lines[0]


class TestRenderMetaData:
    def test_has_instance_id_and_hostname(self) -> None:
        b = _basic_builder()
        spec = _spec("web")
        body = yaml.safe_load(b.render_meta_data(spec, _recipe(b, spec)))
        assert body["instance-id"] == "iid-web"
        assert body["local-hostname"] == "web"


class TestRenderNetworkConfig:
    """The single match-by-MAC netplan (ADR-0017): build NIC + declared NICs."""

    def test_unwrapped_version_2(self) -> None:
        b = _basic_builder()
        body = _netcfg(b, _spec())
        # cloud-init wraps network-config, so it is delivered unwrapped.
        assert body["version"] == 2
        assert "network" not in body

    def test_every_iface_matched_by_mac(self) -> None:
        b = _basic_builder()
        body = _netcfg(b, _spec())
        for cfg in body["ethernets"].values():
            assert "macaddress" in cfg["match"]
            assert "name" not in cfg["match"]

    def test_build_nic_present_and_static(self) -> None:
        b = _basic_builder()
        body = _netcfg(b, _spec(), build_nic=_build_nic("02:00:00:de:ad:01"))
        build0 = body["ethernets"]["build0"]
        assert build0["match"] == {"macaddress": "02:00:00:de:ad:01"}
        # .3 static on the nat build switch: address + DNS + default route via .1.
        assert build0["addresses"] == ["10.97.99.3/24"]
        assert build0["nameservers"] == {"addresses": ["10.97.99.1"]}
        assert build0["routes"] == [{"to": "default", "via": "10.97.99.1"}]

    def test_zero_nic_vm_still_has_build_nic(self) -> None:
        # ORCH-9: a NIC-less VM (no-net) builds with the dedicated build NIC.
        b = _basic_builder()
        spec = VMSpec(name="no-net", devices=[CPU(1), Memory(512), OSDrive("p1", 8)])
        body = yaml.safe_load(
            b.render_network_config(
                spec, _recipe(b, spec), addressing=DEFAULT_ADDR, build_nic=_build_nic(), macs=()
            )
        )
        assert set(body["ethernets"]) == {"build0"}

    def test_declared_nic_matched_by_its_mac(self) -> None:
        b = _basic_builder()
        spec = _spec()
        body = yaml.safe_load(
            b.render_network_config(
                spec,
                _recipe(b, spec),
                addressing=DEFAULT_ADDR,
                build_nic=_build_nic(),
                macs=("02:00:00:11:22:33",),
            )
        )
        assert body["ethernets"]["id0"]["match"] == {"macaddress": "02:00:00:11:22:33"}
        assert body["ethernets"]["id0"]["dhcp4"] is True

    def test_macs_length_must_match_nics(self) -> None:
        b = _basic_builder()
        spec = _spec()  # one NIC
        with pytest.raises(ValueError, match="match-by-MAC"):
            b.render_network_config(
                spec, _recipe(b, spec), addressing=DEFAULT_ADDR, build_nic=_build_nic(), macs=()
            )

    def test_build_nic_no_route_on_isolated_switch(self) -> None:
        # An isolated (no-nat) build switch derives no gateway, so the build NIC
        # is a bare static with no default route (it cannot egress regardless).
        b = _basic_builder()
        isolated = Switch(
            "build", Network("build-net"), cidr="10.97.99.0/24", sidecar=Sidecar(dhcp=True)
        )
        bn = BuildNic(
            mac="02:00:00:aa:bb:cc",
            network="build-net",
            addr=StaticAddr("10.97.99.3"),
            addressing=NetworkAddressing.from_switch(isolated),
        )
        body = _netcfg(b, _spec(), build_nic=bn)
        assert body["ethernets"]["build0"]["addresses"] == ["10.97.99.3/24"]
        assert "routes" not in body["ethernets"]["build0"]


def _static_spec(*nics: NetworkIface) -> VMSpec:
    return VMSpec(
        name="web",
        devices=[CPU(1), Memory(512), OSDrive("p1", 8), *nics],
    )


def _data_spec(*data_sizes: int) -> VMSpec:
    """A one-DHCP-NIC VM with ``data_sizes`` data disks, in order."""
    return VMSpec(
        name="web",
        devices=[
            CPU(1),
            Memory(512),
            OSDrive("p1", 8),
            *(HardDrive("p1", s) for s in data_sizes),
            NetworkIface("netA", addr=DHCPAddr()),
        ],
    )


class TestDeclaredNicNetplan:
    """Per-declared-NIC rendering inside the combined netplan."""

    def test_static_nic_full_derivation(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec = _static_spec(NetworkIface("netA", addr=StaticAddr("172.31.0.50")))
        eth = _netcfg(b, spec)["ethernets"]["id0"]
        assert eth["addresses"] == ["172.31.0.50/24"]
        assert eth["nameservers"] == {"addresses": ["172.31.0.1"]}
        assert eth["routes"] == [{"to": "default", "via": "172.31.0.1"}]
        assert "dhcp4" not in eth

    def test_only_first_declared_static_gets_default_route(self) -> None:
        # Two static NICs: only the first declares the default route. The build
        # NIC's own route is independent (it is inert at run anyway).
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec = _static_spec(
            NetworkIface("netA", addr=StaticAddr("172.31.0.50")),
            NetworkIface("netB", addr=StaticAddr("10.10.10.50")),
        )
        eths = _netcfg(b, spec)["ethernets"]
        assert "routes" in eths["id0"]
        assert "routes" not in eths["id1"]

    def test_mixed_static_dhcp(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec = _static_spec(
            NetworkIface("netA", addr=StaticAddr("172.31.0.50")),
            NetworkIface("netB", addr=DHCPAddr()),
        )
        eths = _netcfg(b, spec)["ethernets"]
        assert eths["id0"]["addresses"] == ["172.31.0.50/24"]
        assert eths["id1"]["dhcp4"] is True

    def test_unconfigured_nic_no_dhcp_wait(self) -> None:
        # addr=None: NIC present, no address. Must NOT emit dhcp4: true (the bug)
        # — leave it to the OS so boot doesn't block on a lease nothing serves.
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec = _static_spec(NetworkIface("netA", addr=None))
        eth = _netcfg(b, spec)["ethernets"]["id0"]
        assert eth["dhcp4"] is False
        assert eth["dhcp6"] is False
        assert eth["optional"] is True
        assert "addresses" not in eth

    def test_explicit_dhcp(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec = _static_spec(NetworkIface("netA", addr=DHCPAddr()))
        eth = _netcfg(b, spec)["ethernets"]["id0"]
        assert eth["dhcp4"] is True
        assert eth["dhcp6"] is False
        assert "addresses" not in eth

    def test_static_dictates_gateway_on_dumb_switch(self) -> None:
        # Dumb L2 switch (no nat/dns => derived gateway/dns are None). A guest at
        # .123 acts as the gateway; the NIC dictates everything via StaticAddr.
        b = CloudInitBuilder(base=CacheEntry("x"))
        bare = {
            "netA": NetworkAddressing(
                cidr="192.168.5.0/24", prefix_len=24, dhcp=False, gateway=None, dns_server=None
            )
        }
        spec = _static_spec(
            NetworkIface(
                "netA",
                addr=StaticAddr("192.168.5.124/24", gw="192.168.5.123", dns=("192.168.5.123",)),
            )
        )
        eth = _netcfg(b, spec, addressing=bare)["ethernets"]["id0"]
        assert eth["addresses"] == ["192.168.5.124/24"]
        assert eth["routes"] == [{"to": "default", "via": "192.168.5.123"}]
        assert eth["nameservers"] == {"addresses": ["192.168.5.123"]}


class TestConfigHash:
    def _hash(
        self,
        b: CloudInitBuilder,
        spec: VMSpec,
        *,
        base_sha: str = "",
        sidecar_sha: str = "",
        build_nic: BuildNic | None = None,
    ) -> str:
        return b.config_hash(
            spec,
            _recipe(b, spec),
            addressing=DEFAULT_ADDR,
            base_sha=base_sha,
            sidecar_sha=sidecar_sha,
            macs=_macs(spec),
            build_nic=build_nic or _build_nic(),
        )

    def test_deterministic(self) -> None:
        b = _basic_builder()
        spec = _spec()
        h1 = self._hash(b, spec, base_sha="abc")
        h2 = self._hash(b, spec, base_sha="abc")
        assert h1 == h2
        assert len(h1) == 16

    def test_base_sha_affects_hash(self) -> None:
        b = _basic_builder()
        spec = _spec()
        assert self._hash(b, spec, base_sha="aaa") != self._hash(b, spec, base_sha="bbb")

    def test_sidecar_sha_affects_hash(self) -> None:
        b = _basic_builder()
        spec = _spec()
        assert self._hash(b, spec, base_sha="z", sidecar_sha="aaa") != self._hash(
            b, spec, base_sha="z", sidecar_sha="bbb"
        )

    def test_build_nic_affects_hash(self) -> None:
        # ADR-0017: the build NIC's MAC/address are baked into the netplan, so a
        # different build NIC keys a different artifact.
        b = _basic_builder()
        spec = _spec()
        assert self._hash(b, spec, build_nic=_build_nic("02:00:00:00:00:01")) != self._hash(
            b, spec, build_nic=_build_nic("02:00:00:00:00:02")
        )

    def test_credentials_affect_hash(self) -> None:
        spec = _spec()
        b1 = CloudInitBuilder(base=CacheEntry("x"), credentials=[PosixCred("u", password="a")])
        b2 = CloudInitBuilder(base=CacheEntry("x"), credentials=[PosixCred("u", password="b")])
        assert self._hash(b1, spec, base_sha="z") != self._hash(b2, spec, base_sha="z")

    def test_os_drive_size_affects_hash(self) -> None:
        b = _basic_builder()
        spec_small = VMSpec(
            name="web",
            devices=[CPU(1), Memory(512), OSDrive("p1", 8), NetworkIface("netA", addr=DHCPAddr())],
        )
        spec_big = VMSpec(
            name="web",
            devices=[CPU(1), Memory(512), OSDrive("p1", 64), NetworkIface("netA", addr=DHCPAddr())],
        )
        assert self._hash(b, spec_small) != self._hash(b, spec_big)

    def test_data_disk_size_affects_hash(self) -> None:
        b = _basic_builder()
        assert self._hash(b, _data_spec(10)) != self._hash(b, _data_spec(20))

    def test_data_disk_count_affects_hash(self) -> None:
        b = _basic_builder()
        assert self._hash(b, _data_spec(10)) != self._hash(b, _data_spec(10, 10))

    def test_data_disk_order_affects_hash(self) -> None:
        # Roles are positional (data0, data1, ...); swapping sizes is a different
        # artifact set.
        b = _basic_builder()
        assert self._hash(b, _data_spec(10, 20)) != self._hash(b, _data_spec(20, 10))

    def test_sensitive_to_declared_static_ipv4(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec_a = _static_spec(NetworkIface("netA", addr=StaticAddr("172.31.0.50")))
        spec_b = _static_spec(NetworkIface("netA", addr=StaticAddr("172.31.0.60")))
        assert self._hash(b, spec_a, base_sha="z") != self._hash(b, spec_b, base_sha="z")

    def test_static_vs_dhcp_differs(self) -> None:
        b = CloudInitBuilder(base=CacheEntry("x"))
        spec_dhcp = _static_spec(NetworkIface("netA", addr=DHCPAddr()))
        spec_static = _static_spec(NetworkIface("netA", addr=StaticAddr("172.31.0.50")))
        assert self._hash(b, spec_dhcp, base_sha="z") != self._hash(b, spec_static, base_sha="z")


class TestRenderSeed:
    def test_seed_bytes_are_iso(self) -> None:
        b = _basic_builder()
        spec = _spec()
        data = b.render_seed(
            spec,
            _recipe(b, spec),
            addressing=DEFAULT_ADDR,
            macs=_macs(spec),
            build_nic=_build_nic(),
        )
        # Standard ISO9660 sig at offset 0x8001
        assert data[0x8001:0x8006] == b"CD001"

    def test_seed_contains_user_data(self) -> None:
        b = _basic_builder()
        spec = _spec()
        data = b.render_seed(
            spec,
            _recipe(b, spec),
            addressing=DEFAULT_ADDR,
            macs=_macs(spec),
            build_nic=_build_nic(),
        )
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
        # The build NIC's MAC is baked into the netplan delivered as network-config.
        assert b"02:00:00:aa:bb:cc" in files["/network-config"]


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

    def test_os_disk_base_returns_base_image(self) -> None:
        # ORCH-5: the orchestrator reads OS-disk origin through this ABC seam.
        base = CacheEntry("debian-13")
        b = CloudInitBuilder(base=base, credentials=[PosixCred("u", password="p")])
        assert b.os_disk_base() is base

    def test_abc_default_is_noop(self) -> None:
        from testrange.builders.base import Builder
        from testrange.credentials.base import Credential

        class _NullBuilder(Builder):
            @property
            def credentials(self) -> tuple[Credential, ...]:
                return ()

            def os_disk_base(self):  # type: ignore[no-untyped-def]
                return None

            def config_hash(  # type: ignore[no-untyped-def]
                self, spec, recipe, *, addressing, base_sha="", sidecar_sha="", macs=(), build_nic
            ):
                return "0" * 16

            def render_seed(self, spec, recipe, *, addressing, macs=(), build_nic):  # type: ignore[no-untyped-def]
                return b""

        b = _NullBuilder()
        spec = _spec()
        ex = _FakeExec()
        b.wait_ready(spec, _recipe(_basic_builder(), spec), ex)
        assert ex.calls == []  # the ABC default never touches execute
