"""Tests for ProxmoxAnswerBuilder — answer.toml, seed ISO, first-boot, config_hash.

The builder installs a PVE node via the auto-installer (installer-origin,
BUILD-1): os_disk_base() is None and boot_media() is the installer ISO. All
provisioning + the build-result contract live in the first-boot script (PVE has
no answer.toml runcmd equivalent).
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path

import pytest

from testrange.builders import ProxmoxAnswerBuilder
from testrange.builders import proxmox as proxmox_mod
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr
from testrange.devices.network import NetworkIface
from testrange.exceptions import BuildNotReadyError
from testrange.guest_io import ExecResult
from testrange.networks import Network, NetworkAddressing, Sidecar, Switch
from testrange.networks.base import BuildNic
from testrange.packages import Apt, Pip
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="pve-test")

_SW = Switch(
    "swA",
    Network("netA"),
    cidr="10.0.5.0/24",
    uplink="lo",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)
ADDR: Mapping[str, NetworkAddressing] = {"netA": NetworkAddressing.from_switch(_SW)}

_BUILD_SW = Switch(
    "build",
    Network("build-net"),
    cidr="10.97.99.0/24",
    uplink="lo",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)


def _build_nic() -> BuildNic:
    return BuildNic(
        mac="02:00:00:aa:bb:cc",
        network="build-net",
        addr=StaticAddr("10.97.99.3"),
        addressing=NetworkAddressing.from_switch(_BUILD_SW),
    )


def _spec(*, static: bool = True, name: str = "pve") -> VMSpec:
    addr = StaticAddr("10.0.5.20") if static else DHCPAddr()
    return VMSpec(
        name=name,
        firmware="uefi",
        devices=[CPU(2), Memory(2048), OSDrive("p1", 16), NetworkIface("netA", addr=addr)],
    )


def _recipe(builder: ProxmoxAnswerBuilder, spec: VMSpec) -> VMRecipe:
    return VMRecipe(spec=spec, builder=builder, communicator=SSHCommunicator("root"))


def _builder(**kw: object) -> ProxmoxAnswerBuilder:
    params: dict[str, object] = {
        "installer_iso": CacheEntry("pve-9-iso"),
        "credentials": [PosixCred("root", password="rootpass", ssh_key=_KEY)],
        "packages": [Apt("htop"), Pip("requests")],
        "post_install_commands": ("echo hi > /tmp/hi",),
    }
    params.update(kw)
    return ProxmoxAnswerBuilder(**params)  # type: ignore[arg-type]


def _answer(b: ProxmoxAnswerBuilder, spec: VMSpec) -> str:
    return b.build_answer_toml(spec, _recipe(b, spec), addressing=ADDR)


def _config_hash(b: ProxmoxAnswerBuilder, spec: VMSpec, **kw: object) -> str:
    return b.config_hash(spec, _recipe(b, spec), addressing=ADDR, build_nic=_build_nic(), **kw)  # type: ignore[arg-type]


class _FakeExec:
    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, *, timeout=60.0, cwd=None):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))
        return ExecResult(exit_code=self._exit_code, stdout=b"", stderr=b"boom", duration=0.0)


class TestConstruction:
    def test_requires_root_credential(self) -> None:
        with pytest.raises(ValueError, match="requires a root Credential"):
            ProxmoxAnswerBuilder(
                installer_iso=CacheEntry("x"), credentials=[PosixCred("u", password="p")]
            )

    def test_root_must_have_password(self) -> None:
        # A root with only an SSH key can't satisfy answer.toml's mandatory
        # root-password.
        with pytest.raises(ValueError, match="must be a PosixCred with a password"):
            ProxmoxAnswerBuilder(
                installer_iso=CacheEntry("x"), credentials=[PosixCred("root", ssh_key=_KEY)]
            )

    def test_rejects_non_apt_pip_package(self) -> None:
        class _Weird:
            name = "weird"

        with pytest.raises(ValueError, match="must be Apt or Pip"):
            _builder(packages=[_Weird()])

    def test_boot_media_is_installer_iso(self) -> None:
        # os_disk_base() is statically None (installer-origin) — mypy enforces
        # the type; the runtime installer-origin path is covered end-to-end in
        # test_build_phase.test_installer_origin_*.
        assert _builder().boot_media() == CacheEntry("pve-9-iso")


class TestAnswerToml:
    def test_core_fields(self) -> None:
        a = _answer(_builder(), _spec())
        assert 'root-password = "rootpass"' in a
        assert 'fqdn = "pve.testrange.local"' in a
        assert 'country = "us"' in a
        assert "[disk-setup]" in a
        assert 'disk-list = ["vda"]' in a
        assert 'source = "from-iso"' in a  # [first-boot]
        assert 'ordering = "network-online"' in a

    def test_static_network_from_answer(self) -> None:
        a = _answer(_builder(), _spec(static=True))
        assert 'source = "from-answer"' in a
        assert 'cidr = "10.0.5.20/24"' in a
        assert 'gateway = "10.0.5.1"' in a  # sidecar .1 (nat), from the subnet
        assert 'dns = "10.0.5.1"' in a  # sidecar .1 (dns), from the subnet
        assert 'filter.ID_NET_NAME = "enp1s0"' in a

    def test_static_gw_and_dns_override_subnet(self) -> None:
        # An explicit StaticAddr gw/dns wins over the sidecar-derived subnet values.
        spec = VMSpec(
            name="pve",
            firmware="uefi",
            devices=[
                CPU(2),
                Memory(2048),
                OSDrive("p1", 16),
                NetworkIface(
                    "netA", addr=StaticAddr("10.0.5.20", gw="10.0.5.254", dns=("9.9.9.9",))
                ),
            ],
        )
        a = _answer(_builder(), spec)
        assert 'gateway = "10.0.5.254"' in a
        assert 'dns = "9.9.9.9"' in a

    def test_dhcp_fallback_when_no_static(self) -> None:
        a = _answer(_builder(), _spec(static=False))
        assert 'source = "from-dhcp"' in a
        assert "from-answer" not in a

    def test_dhcp_fallback_when_no_nic(self) -> None:
        # A spec with zero NICs is valid; _network_block falls back to DHCP.
        spec = VMSpec(
            name="pve", firmware="uefi", devices=[CPU(2), Memory(2048), OSDrive("p1", 16)]
        )
        a = _answer(_builder(), spec)
        assert 'source = "from-dhcp"' in a
        assert "from-answer" not in a

    def test_network_interface_propagates_to_answer_and_flip(self) -> None:
        # network_interface must drive BOTH answer.toml's filter AND the
        # first-boot network flip, or the flip flushes the wrong NIC (BUILD-15).
        b = _builder(network_interface="ens18")
        a = _answer(b, _spec(static=True))
        assert 'filter.ID_NET_NAME = "ens18"' in a
        s = b._first_boot_script()
        assert 'NIC="ens18"' in s
        assert 'NIC="enp1s0"' not in s

    def test_ssh_keys_included(self) -> None:
        a = _answer(_builder(), _spec())
        assert "root-ssh-keys = [" in a
        assert _KEY.auth_line in a

    def test_no_ssh_keys_when_absent(self) -> None:
        b = _builder(credentials=[PosixCred("root", password="rootpass")])
        assert "root-ssh-keys" not in _answer(b, _spec())


class TestFirstBootScript:
    def test_build_result_contract(self) -> None:
        s = _builder()._first_boot_script()
        assert "trap __tr_emit_fail ERR" in s
        assert "set -eE" in s
        assert "TESTRANGE-RESULT: ok" in s
        assert "/dev/ttyS0" in s
        assert "systemctl poweroff" in s

    def test_network_flip_and_repo_swap(self) -> None:
        s = _builder()._first_boot_script()
        assert "dhclient -1 -v" in s
        assert "pve-no-subscription" in s
        assert "pve-enterprise.list" in s

    def test_threads_packages_and_commands(self) -> None:
        s = _builder()._first_boot_script()
        assert "apt-get install -y htop" in s
        assert "pip3 install" in s and "requests" in s
        assert "echo hi > /tmp/hi" in s

    def test_apt_insecure_prologue(self) -> None:
        plain = _builder()._first_boot_script()
        insecure = _builder(apt_insecure=True)._first_boot_script()
        assert "99-testrange-insecure" not in plain
        assert "99-testrange-insecure" in insecure
        # The TLS-skip conf is removed before capture so it does not survive into
        # the installed run-phase image.
        assert "rm -f /etc/apt/apt.conf.d/99-testrange-insecure" in insecure


class TestConfigHash:
    def test_deterministic(self) -> None:
        b = _builder()
        spec = _spec()
        assert _config_hash(b, spec, base_sha="abc") == _config_hash(b, spec, base_sha="abc")

    def test_len_16_hex(self) -> None:
        h = _config_hash(_builder(), _spec(), base_sha="abc")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_sensitive_to_base_sha(self) -> None:
        b = _builder()
        spec = _spec()
        assert _config_hash(b, spec, base_sha="aaa") != _config_hash(b, spec, base_sha="bbb")

    def test_sensitive_to_static_ip(self) -> None:
        b = _builder()
        spec_a = VMSpec(
            name="pve",
            firmware="uefi",
            devices=[
                CPU(2),
                Memory(2048),
                OSDrive("p1", 16),
                NetworkIface("netA", addr=StaticAddr("10.0.5.20")),
            ],
        )
        spec_b = VMSpec(
            name="pve",
            firmware="uefi",
            devices=[
                CPU(2),
                Memory(2048),
                OSDrive("p1", 16),
                NetworkIface("netA", addr=StaticAddr("10.0.5.99")),
            ],
        )
        assert _config_hash(b, spec_a, base_sha="x") != _config_hash(b, spec_b, base_sha="x")

    def test_sensitive_to_packages(self) -> None:
        spec = _spec()
        h1 = _config_hash(_builder(packages=[Apt("htop")]), spec, base_sha="x")
        h2 = _config_hash(_builder(packages=[Apt("nginx")]), spec, base_sha="x")
        assert h1 != h2

    def test_sensitive_to_ssh_key_value(self) -> None:
        # The keys are baked into the answer file's root-ssh-keys and not re-seeded
        # at run, so a different key MUST bust the cache (CORE-64) — else a plan
        # with a new key cache-hits a disk it can't log into.
        spec = _spec()
        other = SSHKey.generate(comment="rotated")
        h1 = _config_hash(
            _builder(credentials=[PosixCred("root", password="rootpass", ssh_key=_KEY)]),
            spec,
            base_sha="x",
        )
        h2 = _config_hash(
            _builder(credentials=[PosixCred("root", password="rootpass", ssh_key=other)]),
            spec,
            base_sha="x",
        )
        assert h1 != h2

    def test_sensitive_to_root_password(self) -> None:
        spec = _spec()
        h1 = _config_hash(
            _builder(credentials=[PosixCred("root", password="a")]), spec, base_sha="x"
        )
        h2 = _config_hash(
            _builder(credentials=[PosixCred("root", password="b")]), spec, base_sha="x"
        )
        assert h1 != h2


class TestSeedIso:
    def test_seed_iso_carries_answer_toml(self) -> None:
        pycdlib = pytest.importorskip("pycdlib")
        b = _builder()
        spec = _spec()
        data = b.render_seed(spec, _recipe(b, spec), addressing=ADDR, build_nic=_build_nic())
        assert isinstance(data, bytes) and len(data) > 0
        iso = pycdlib.PyCdlib()
        iso.open_fp(io.BytesIO(data))
        try:
            assert iso.pvd.volume_identifier.decode("ascii").rstrip().upper().startswith("PROXMOX")
            out = io.BytesIO()
            iso.get_file_from_iso_fp(out, joliet_path="/answer.toml")
            assert b"root-password" in out.getvalue()
        finally:
            iso.close()


class TestPrepareBootMedia:
    def test_prepares_once_and_caches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Path, Path]] = []

        def fake_prepare(vanilla, out, *, partition_label, first_boot_script):  # type: ignore[no-untyped-def]
            calls.append((vanilla, out))
            Path(out).write_bytes(b"PREPARED")

        monkeypatch.setattr(proxmox_mod, "prepare_iso", fake_prepare)
        vanilla = tmp_path / "pve.iso"
        vanilla.write_bytes(b"VANILLA")
        b = _builder()

        p1 = b.prepare_boot_media(vanilla)
        assert p1.exists() and p1.read_bytes() == b"PREPARED"
        assert len(calls) == 1
        # Second call reuses the cached prepared ISO (no re-prep).
        p2 = b.prepare_boot_media(vanilla)
        assert p2 == p1
        assert len(calls) == 1

    def test_prepared_path_keys_on_first_boot_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            proxmox_mod,
            "prepare_iso",
            lambda v, out, **kw: Path(out).write_bytes(b"x"),
        )
        vanilla = tmp_path / "pve.iso"
        vanilla.write_bytes(b"VANILLA")
        p_plain = _builder(packages=[Apt("htop")]).prepare_boot_media(vanilla)
        p_other = _builder(packages=[Apt("nginx")]).prepare_boot_media(vanilla)
        # Different provisioning => different first-boot script => different ISO.
        assert p_plain != p_other


class TestWaitReady:
    def test_ok_when_ssh_live(self) -> None:
        b = _builder()
        ex = _FakeExec(exit_code=0)
        b.wait_ready(_spec(), _recipe(b, _spec()), ex)
        assert len(ex.calls) == 1  # a single liveness probe; the exact argv is incidental

    def test_raises_when_unreachable(self) -> None:
        b = _builder()
        with pytest.raises(BuildNotReadyError, match="not reachable over SSH"):
            b.wait_ready(_spec(), _recipe(b, _spec()), _FakeExec(exit_code=1))


# A trimmed copy of PVE 9.x /boot/grub/grub.cfg: the automated entry (the only one
# carrying ``proxmox-start-auto-installer``) plus a neighbor that must stay intact.
_PVE_GRUB_CFG = """\
if [ -f auto-installer-mode.toml ]; then
    menuentry 'Install Proxmox VE (Automated)' --class debian {
        linux       /boot/linux26 ro ramdisk_size=16777216 rw quiet splash=silent proxmox-start-auto-installer
        initrd      /boot/initrd.img
    }
fi
menuentry 'Install Proxmox VE (Graphical)' --class debian {
    linux	/boot/linux26 ro ramdisk_size=16777216 rw quiet splash=silent
    initrd	/boot/initrd.img
}
"""


class TestGrubSerialConsole:
    """The grub rewrite that makes the auto-installer observable on ttyS0."""

    def _auto_line(self, cfg: str) -> str:
        from testrange.builders._proxmox_prepare import _AUTO_INSTALLER_GRUB_TOKEN

        return next(
            ln for ln in cfg.splitlines()
            if _AUTO_INSTALLER_GRUB_TOKEN in ln and ln.lstrip().startswith("linux")
        )

    def test_grafts_serial_console_onto_automated_entry(self) -> None:
        from testrange.builders._proxmox_prepare import _grub_with_serial_console

        out = _grub_with_serial_console(_PVE_GRUB_CFG)
        auto = self._auto_line(out)
        assert "console=ttyS0,115200" in auto
        # quiet/splash=silent give way to splash=verbose so the install is visible.
        assert "quiet" not in auto
        assert "splash=silent" not in auto and "splash=verbose" in auto
        # The auto-installer activation token must survive — it is what selects
        # unattended mode; without it the rewrite would break the install.
        assert "proxmox-start-auto-installer" in auto

    def test_leaves_other_entries_untouched(self) -> None:
        from testrange.builders._proxmox_prepare import _grub_with_serial_console

        out = _grub_with_serial_console(_PVE_GRUB_CFG)
        # The graphical entry has no auto-installer token, so it keeps its console
        # (no ttyS0) and its quiet/splash=silent — only the automated line changed.
        graphical = next(
            ln for ln in out.splitlines()
            if ln.lstrip().startswith("linux") and "proxmox-start-auto-installer" not in ln
        )
        assert "console=ttyS0" not in graphical
        assert "quiet splash=silent" in graphical

    def test_idempotent(self) -> None:
        from testrange.builders._proxmox_prepare import _grub_with_serial_console

        once = _grub_with_serial_console(_PVE_GRUB_CFG)
        assert _grub_with_serial_console(once) == once

    def test_raises_when_automated_entry_absent(self) -> None:
        # A grub.cfg that exists but has no auto-installer entry is a PVE layout we
        # no longer recognize — fail loud rather than ship a serial-blind ISO.
        from testrange.builders._proxmox_prepare import (
            ProxmoxPrepareError,
            _grub_with_serial_console,
        )

        with pytest.raises(ProxmoxPrepareError, match="auto-installer grub entry not found"):
            _grub_with_serial_console("menuentry 'Something Else' {\n    linux /boot/x\n}\n")


class TestPrepareRecipeBustsCache:
    """A prepare-recipe bump must change the prepared-ISO path (cache invalidation)."""

    def test_recipe_version_folds_into_prepared_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            proxmox_mod,
            "prepare_iso",
            lambda v, out, **kw: Path(out).write_bytes(b"x"),
        )
        vanilla = tmp_path / "pve.iso"
        vanilla.write_bytes(b"VANILLA")
        before = _builder().prepare_boot_media(vanilla)
        # Simulate a future recipe change: same inputs, new recipe tag => new path,
        # so a stale cached copy from the prior recipe is never reused.
        monkeypatch.setattr(proxmox_mod, "PREPARE_ISO_RECIPE", "some-newer-recipe-v2")
        after = _builder().prepare_boot_media(vanilla)
        assert before.name != after.name
