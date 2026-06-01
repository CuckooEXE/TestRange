"""Tests for ESXiKickstartBuilder — kickstart rendering, config_hash, ISO prep.

Installer-origin (BUILD-1), single-CDROM: os_disk_base() is None, boot_media() is
the installer ISO, render_seed() is None (ks.cfg rides the boot media). The
build-result contract lives in the kickstart %firstboot block.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from testrange.builders import ESXiKickstartBuilder
from testrange.builders import esxi as esxi_mod
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.exceptions import BuildNotReadyError
from testrange.guest_io import ExecResult
from testrange.networks import Network, NetworkAddressing, Sidecar, Switch
from testrange.networks.base import BuildNic
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="esxi-test")

_SW = Switch("swA", Network("netA"), cidr="10.0.5.0/24", sidecar=Sidecar(dhcp=True))
ADDR: Mapping[str, NetworkAddressing] = {"netA": NetworkAddressing.from_switch(_SW)}
_BUILD_SW = Switch("build", Network("bnet"), cidr="10.97.99.0/24", sidecar=Sidecar(dhcp=True))


def _build_nic() -> BuildNic:
    return BuildNic(
        mac="02:00:00:aa:bb:cc",
        network="bnet",
        addr=StaticAddr("10.97.99.3"),
        addressing=NetworkAddressing.from_switch(_BUILD_SW),
    )


def _spec(*, firmware: str = "bios", disk_gb: int = 40, name: str = "esxi") -> VMSpec:
    return VMSpec(
        name=name,
        firmware=firmware,
        devices=[CPU(2), Memory(4096), OSDrive("p1", disk_gb), NetworkIface("netA")],
    )


def _recipe(b: ESXiKickstartBuilder, spec: VMSpec) -> VMRecipe:
    return VMRecipe(spec=spec, builder=b, communicator=SSHCommunicator("root"))


def _builder(**kw: object) -> ESXiKickstartBuilder:
    params: dict[str, object] = {
        "installer_iso": CacheEntry("esxi-8-iso"),
        "credentials": [PosixCred("root", password="VMware1!", ssh_key=_KEY)],
    }
    params.update(kw)
    return ESXiKickstartBuilder(**params)  # type: ignore[arg-type]


def _config_hash(b: ESXiKickstartBuilder, spec: VMSpec, **kw: object) -> str:
    return b.config_hash(spec, _recipe(b, spec), addressing=ADDR, build_nic=_build_nic(), **kw)  # type: ignore[arg-type]


class _FakeExec:
    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, *, timeout=60.0, cwd=None):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))
        return ExecResult(exit_code=self._exit_code, stdout=b"", stderr=b"x", duration=0.0)


class TestConstruction:
    def test_requires_root(self) -> None:
        with pytest.raises(ValueError, match="requires a root Credential"):
            ESXiKickstartBuilder(
                installer_iso=CacheEntry("x"), credentials=[PosixCred("u", password="p")]
            )

    def test_root_must_have_password(self) -> None:
        with pytest.raises(ValueError, match="non-empty password"):
            ESXiKickstartBuilder(
                installer_iso=CacheEntry("x"), credentials=[PosixCred("root", ssh_key=_KEY)]
            )

    def test_boot_media_is_installer_iso(self) -> None:
        # os_disk_base() and render_seed() are statically None (installer-origin,
        # single-CDROM) — mypy enforces the types; the runtime no-seed build path
        # is covered in test_build_phase.test_installer_origin_with_no_seed.
        assert _builder().boot_media() == CacheEntry("esxi-8-iso")


class TestKickstart:
    def test_core_directives(self) -> None:
        ks = _builder()._render_kickstart()
        assert "accepteula" in ks
        assert "rootpw VMware1!" in ks
        assert "install --firstdisk --overwritevmfs" in ks
        assert "network --bootproto=dhcp --device=vmnic0" in ks
        assert "%firstboot --interpreter=busybox" in ks

    def test_build_result_contract(self) -> None:
        ks = _builder()._render_kickstart()
        assert "TESTRANGE-RESULT: ok" in ks
        assert "TESTRANGE-RESULT: fail" in ks
        assert "/dev/ttyS0" in ks
        assert "esxcli system shutdown poweroff" in ks

    def test_ssh_block_when_key_present(self) -> None:
        ks = _builder()._render_kickstart()
        assert "vim-cmd hostsvc/enable_ssh" in ks
        assert "/etc/ssh/keys-root/authorized_keys" in ks
        assert _KEY.auth_line in ks
        assert "ruleset-id=sshServer" in ks

    def test_no_ssh_block_without_key(self) -> None:
        b = _builder(credentials=[PosixCred("root", password="VMware1!")])
        ks = b._render_kickstart()
        assert "enable_ssh" not in ks
        assert "TESTRANGE-RESULT: ok" in ks  # still reports + powers off


class TestConfigHash:
    def test_deterministic_and_hex16(self) -> None:
        b, spec = _builder(), _spec()
        h = _config_hash(b, spec, base_sha="a")
        assert h == _config_hash(b, spec, base_sha="a")
        assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)

    def test_sensitive_to_base_sha_password_firmware_disk(self) -> None:
        b = _builder()
        base = _config_hash(b, _spec(), base_sha="a")
        assert base != _config_hash(b, _spec(), base_sha="b")
        assert base != _config_hash(
            _builder(credentials=[PosixCred("root", password="Other1!")]), _spec(), base_sha="a"
        )
        assert base != _config_hash(b, _spec(firmware="uefi"), base_sha="a")
        assert base != _config_hash(b, _spec(disk_gb=60), base_sha="a")

    def test_insensitive_to_ssh_key_rotation(self) -> None:
        spec = _spec()
        other = SSHKey.generate(comment="rotated")
        h1 = _config_hash(
            _builder(credentials=[PosixCred("root", password="VMware1!", ssh_key=_KEY)]),
            spec,
            base_sha="a",
        )
        h2 = _config_hash(
            _builder(credentials=[PosixCred("root", password="VMware1!", ssh_key=other)]),
            spec,
            base_sha="a",
        )
        assert h1 == h2

    def test_sensitive_to_ssh_presence(self) -> None:
        spec = _spec()
        with_key = _config_hash(_builder(), spec, base_sha="a")
        without = _config_hash(
            _builder(credentials=[PosixCred("root", password="VMware1!")]), spec, base_sha="a"
        )
        assert with_key != without

    def test_undersized_disk_rejected(self) -> None:
        with pytest.raises(ValueError, match="OSDrive >= 33"):
            _config_hash(_builder(), _spec(disk_gb=20), base_sha="a")


class TestPrepareBootMedia:
    def test_prepares_once_and_caches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Path] = []

        def fake_prepare(vanilla, out, *, kickstart):  # type: ignore[no-untyped-def]
            calls.append(out)
            Path(out).write_bytes(b"PREPARED")

        monkeypatch.setattr(esxi_mod, "prepare_iso", fake_prepare)
        vanilla = tmp_path / "esxi.iso"
        vanilla.write_bytes(b"VANILLA")
        b = _builder()
        p1 = b.prepare_boot_media(vanilla)
        assert p1.read_bytes() == b"PREPARED" and len(calls) == 1
        assert b.prepare_boot_media(vanilla) == p1 and len(calls) == 1  # cached


class TestWaitReady:
    def test_ok(self) -> None:
        b = _builder()
        ex = _FakeExec(0)
        b.wait_ready(_spec(), _recipe(b, _spec()), ex)
        assert ex.calls == [("true",)]

    def test_unreachable_raises(self) -> None:
        b = _builder()
        with pytest.raises(BuildNotReadyError, match="not reachable over SSH"):
            b.wait_ready(_spec(), _recipe(b, _spec()), _FakeExec(1))
