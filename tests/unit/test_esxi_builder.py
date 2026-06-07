"""Tests for ESXiKickstartBuilder — kickstart rendering, config_hash, ISO prep.

Installer-origin (BUILD-1), single-CDROM: os_disk_base() is None, boot_media() is
the installer ISO, render_seed() is None (ks.cfg rides the boot media). The
build-result contract lives in the kickstart %firstboot block.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from testrange.builders import ESXiKickstartBuilder
from testrange.builders import _esxi_prepare as prep
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
from testrange.orchestrator.build_phase import parse_build_result
from testrange.utils import EcdsaKey, SSHKey
from testrange.vms import VMRecipe, VMSpec

# ESXi's FIPS sshd rejects Ed25519, so the builder requires ECDSA/RSA (CORE-63).
_KEY = EcdsaKey.generate(comment="esxi-test")

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

    def test_ed25519_root_key_rejected(self) -> None:
        # ESXi 8 FIPS sshd silently denies Ed25519; the builder must fail loud.
        with pytest.raises(ValueError, match=r"Ed25519.*FIPS"):
            _builder(
                credentials=[PosixCred("root", password="VMware1!", ssh_key=SSHKey.generate())]
            )

    def test_ecdsa_root_key_accepted(self) -> None:
        b = _builder(credentials=[PosixCred("root", password="VMware1!", ssh_key=_KEY)])
        assert b.credentials[0].username == "root"

    def test_password_with_newline_rejected(self) -> None:
        # A newline would break out of the ks.cfg `rootpw` line.
        with pytest.raises(ValueError, match="control characters"):
            _builder(credentials=[PosixCred("root", password="oops\ninjected")])

    def test_password_with_control_char_rejected(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _builder(credentials=[PosixCred("root", password="bad\x07bell")])

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

    def test_build_result_emitted_from_post_via_vsish(self) -> None:
        # ESXi has no userspace serial write, so the record is injected into the
        # installer vmkernel log (-> COM1 via logPort) from %post, then the
        # installer is hard-powered-off. The old /dev/ttyS0 echo + esxcli poweroff
        # (which hung — the ESXI-17 bug) are gone.
        ks = _builder()._render_kickstart()
        assert "%post --interpreter=busybox" in ks
        assert "vsish -e set /system/log" in ks
        assert "poweroff -f" in ks
        assert "/dev/ttyS0" not in ks
        assert "esxcli system shutdown poweroff" not in ks

    def test_marker_never_verbatim_in_source(self) -> None:
        # weasel echoes every ks.cfg section body to the same serial at parse time,
        # so a literal `TESTRANGE-RESULT:` in the SOURCE would false-trigger the
        # orchestrator's parser before the real emission. The marker must be
        # assembled at runtime from shell vars and never appear verbatim.
        ks = _builder()._render_kickstart()
        assert "TESTRANGE-RESULT:" not in ks
        # ...but the runtime-assembled emission is present.
        assert '"${_t}-${_r}: ok"' in ks
        assert "_t=TESTRANGE" in ks
        assert "_r=RESULT" in ks

    def test_emitted_marker_parses_as_ok(self) -> None:
        # The line vsish puts on the wire is `<ts> cpuN:NNN)TESTRANGE-RESULT: ok`.
        # Tie the builder's marker to the orchestrator parser: it must read `ok`.
        emitted = b"2026-01-01T00:00:00.000Z cpu0:12345)TESTRANGE-RESULT: ok\n"
        result = parse_build_result(emitted)
        assert result is not None and result.ok

    def test_ssh_block_when_key_present(self) -> None:
        ks = _builder()._render_kickstart()
        assert "%firstboot --interpreter=busybox" in ks
        assert "/etc/ssh/keys-root/authorized_keys" in ks
        assert _KEY.auth_line in ks
        # sshd enable is deferred to rc.local.d (vim-cmd in %firstboot runs before
        # hostd and hangs); the ruleset is opened there too.
        assert "/etc/rc.local.d/local.sh" in ks
        assert "vim-cmd hostsvc/enable_ssh" in ks
        assert "--ruleset-id sshServer" in ks

    def test_heredoc_terminators_and_key_at_column_zero(self) -> None:
        # busybox only closes a plain `cat <<'EOF'` heredoc on a line that is
        # *exactly* the terminator (no leading whitespace). An indented %firstboot
        # body would let a heredoc swallow the rest of the script. Guard the flat
        # layout for both the key file and the rc.local.d block.
        ks = _builder()._render_kickstart()
        lines = ks.splitlines()
        assert "KEYEOF" in lines  # exact, column-0 terminator (not "  KEYEOF")
        assert "RCEOF" in lines
        assert _KEY.auth_line in lines  # key written without leading whitespace

    def test_no_firstboot_block_without_key(self) -> None:
        b = _builder(credentials=[PosixCred("root", password="VMware1!")])
        ks = b._render_kickstart()
        assert "%firstboot" not in ks  # no run-phase provisioning to do
        assert "enable_ssh" not in ks
        # the build-result emission lives in %post and is independent of the key.
        assert "vsish -e set /system/log" in ks
        assert "poweroff -f" in ks


class TestLicense:
    def test_serialnum_emitted_when_license_set(self) -> None:
        ks = _builder(license="HG00K-03H8K-48929-8K1NP-3LUJ4")._render_kickstart()
        assert "serialnum --esx=HG00K-03H8K-48929-8K1NP-3LUJ4" in ks
        # Top-level directive (install section), not a %firstboot esxcli/vim-cmd call.
        assert ks.index("serialnum") < ks.index("%firstboot")

    def test_no_serialnum_without_license(self) -> None:
        assert "serialnum" not in _builder()._render_kickstart()

    def test_license_with_newline_rejected(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _builder(license="HG00K\ninjected")

    def test_empty_license_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty license key"):
            _builder(license="   ")


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

    def test_sensitive_to_license(self) -> None:
        spec = _spec()
        unlicensed = _config_hash(_builder(), spec, base_sha="a")
        licensed = _config_hash(
            _builder(license="HG00K-03H8K-48929-8K1NP-3LUJ4"), spec, base_sha="a"
        )
        other = _config_hash(_builder(license="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"), spec, base_sha="a")
        assert unlicensed != licensed != other != unlicensed

    def test_sensitive_to_ssh_key_value(self) -> None:
        # The key is baked into %firstboot's authorized_keys and not re-seeded at
        # run, so a different key MUST bust the cache (CORE-64) — else a plan with
        # a new key cache-hits a disk it can't log into.
        spec = _spec()
        other = EcdsaKey.generate(comment="rotated")
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
        assert h1 != h2

    def test_sensitive_to_ssh_presence(self) -> None:
        spec = _spec()
        with_key = _config_hash(_builder(), spec, base_sha="a")
        without = _config_hash(
            _builder(credentials=[PosixCred("root", password="VMware1!")]), spec, base_sha="a"
        )
        assert with_key != without


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
        assert len(ex.calls) == 1  # a single liveness probe; the exact argv is incidental

    def test_unreachable_raises(self) -> None:
        b = _builder()
        with pytest.raises(BuildNotReadyError, match="not reachable over SSH"):
            b.wait_ready(_spec(), _recipe(b, _spec()), _FakeExec(1))


class TestPatchBootcfg:
    """`_patch_bootcfg` is pure file I/O (no xorriso) — unit-testable directly."""

    def test_rewrites_kernelopt_and_drops_cdromboot(self, tmp_path: Path) -> None:
        p = tmp_path / "BOOT.CFG"
        p.write_text("title=x\nkernelopt=cdromBoot runweasel\nbuild=1\n")
        prep._patch_bootcfg(p)
        out = p.read_text()
        assert "kernelopt=runweasel ks=cdrom:/ks.cfg" in out
        assert "cdromBoot" not in out

    def test_idempotent_when_already_patched(self, tmp_path: Path) -> None:
        p = tmp_path / "BOOT.CFG"
        p.write_text("title=x\nkernelopt=runweasel ks=cdrom:/ks.cfg\n")
        before = p.read_text()
        prep._patch_bootcfg(p)
        assert p.read_text() == before

    def test_appends_when_no_kernelopt_line(self, tmp_path: Path) -> None:
        p = tmp_path / "BOOT.CFG"
        p.write_text("title=x\nbuild=1\n")
        prep._patch_bootcfg(p)
        assert "kernelopt=runweasel ks=cdrom:/ks.cfg" in p.read_text()

    def test_preserves_crlf_line_endings(self, tmp_path: Path) -> None:
        p = tmp_path / "BOOT.CFG"
        p.write_bytes(b"title=x\r\nkernelopt=cdromBoot\r\n")
        prep._patch_bootcfg(p)
        data = p.read_bytes()
        assert b"\r\n" in data
        # No bare LFs survive (every newline stayed CRLF).
        assert b"\n" not in data.replace(b"\r\n", b"")


class TestPrepErrorPaths:
    def test_missing_xorriso_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # prepare_iso references the module-level `shutil`, so patching the real
        # shutil.which (no `prep.shutil` attr access — mypy --strict rejects that)
        # exercises the missing-binary branch.
        monkeypatch.setattr(shutil, "which", lambda _: None)
        with pytest.raises(prep.EsxiPrepareError, match="xorriso not found"):
            prep.prepare_iso(tmp_path / "in.iso", tmp_path / "out.iso", kickstart="ks")

    def test_extract_partial_output_on_failure_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BUILD-19: a non-zero xorriso exit that left a *partial* file behind
        # must raise (not be mistaken for a clean extraction), and the partial
        # must be removed. (subprocess is patched by dotted path so the test file
        # need not import the project-banned `subprocess`.)
        out = tmp_path / "BOOT.CFG"

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            out.write_bytes(b"truncated")  # xorriso left a partial extraction
            return SimpleNamespace(
                returncode=32, stdout="", stderr="xorriso : FAILURE : write error"
            )

        monkeypatch.setattr("testrange.builders._esxi_prepare.subprocess.run", fake_run)
        with pytest.raises(prep.EsxiPrepareError, match="exit 32"):
            prep._extract("xorriso", tmp_path / "in.iso", "/EFI/BOOT/BOOT.CFG", out)
        assert not out.exists()

    def test_extract_absent_source_is_not_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "BOOT.CFG"

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="xorriso : FAILURE : file object not found"
            )

        monkeypatch.setattr("testrange.builders._esxi_prepare.subprocess.run", fake_run)
        assert prep._extract("xorriso", tmp_path / "in.iso", "/EFI/BOOT/BOOT.CFG", out) is False
