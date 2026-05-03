"""Unit tests for :class:`~testrange.vms.builders.ProxmoxAnswerBuilder`.

Shape matches :mod:`tests.test_builders` and :mod:`tests.test_unattend`:
small, focused classes per concern, ``MagicMock`` for the cache and
storage interfaces, ``monkeypatch`` for the pycdlib write so we can
inspect the calls instead of producing actual ISO bytes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange import VM, Credential, HardDrive, Memory, vNIC, vCPU
from testrange.exceptions import CloudInitError
from testrange.vms.builders import Builder
from testrange.vms.builders.base import InstallDomain, RunDomain
from testrange.vms.builders.proxmox_answer import (
    ProxmoxAnswerBuilder,
    _primary_network_ref,
    _root_credential,
    _toml_str,
    build_proxmox_seed_iso_bytes,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _proxmox_vm(**overrides: Any) -> VM:
    """A minimal ProxMox VM spec.  Defaults track the example so any
    drift between defaults and the lived-in usage is loud."""
    defaults: dict[str, Any] = dict(
        name="proxmox",
        iso="https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso",
        users=[Credential("root", "testrange", ssh_key="ssh-ed25519 AAA root@h")],
        communicator="ssh",
        devices=[
            vCPU(2),
            Memory(4),
            HardDrive(64),
            vNIC("OuterNet", ip="10.0.0.10"),
        ],
    )
    defaults.update(overrides)
    return VM(**defaults)


# ----------------------------------------------------------------------
# Builder ABC contract
# ----------------------------------------------------------------------


class TestProxmoxAnswerBuilderContract:
    def test_is_a_builder(self) -> None:
        assert issubclass(ProxmoxAnswerBuilder, Builder)
        assert isinstance(ProxmoxAnswerBuilder(), Builder)

    def test_default_communicator_is_ssh(self) -> None:
        # PVE doesn't ship qemu-guest-agent in the base install; SSH is
        # the only reliable channel without a post-install hook.
        assert ProxmoxAnswerBuilder().default_communicator() == "ssh"

    def test_needs_install_phase(self) -> None:
        assert ProxmoxAnswerBuilder().needs_install_phase() is True

    def test_does_not_need_boot_keypress(self) -> None:
        # Unlike the Windows installer, PVE's GRUB auto-enters the
        # default entry without prompting.
        assert ProxmoxAnswerBuilder().needs_boot_keypress() is False

    def test_default_uefi_is_true(self) -> None:
        # SeaBIOS + q35 + SATA-CD triple-faults BIOS GRUB on the PVE
        # ISO; UEFI is the documented workaround and the v0 default.
        assert ProxmoxAnswerBuilder().uefi is True


# ----------------------------------------------------------------------
# cache_key — folds in users, packages, post-install, disk size, AND
# the [network] block content (a difference there changes the
# installed system, so cache must split).
# ----------------------------------------------------------------------


class TestProxmoxAnswerBuilderCacheKey:
    def test_returns_24_hex_chars(self) -> None:
        key = ProxmoxAnswerBuilder().cache_key(_proxmox_vm())
        assert len(key) == 24
        assert all(c in "0123456789abcdef" for c in key)

    def test_deterministic_across_instances(self) -> None:
        vm = _proxmox_vm()
        b1 = ProxmoxAnswerBuilder()
        b2 = ProxmoxAnswerBuilder()
        assert b1.cache_key(vm) == b2.cache_key(vm)

    def test_changes_with_iso(self) -> None:
        b = ProxmoxAnswerBuilder()
        a = _proxmox_vm(iso="https://x/proxmox-ve_9.0-1.iso")
        c = _proxmox_vm(iso="https://x/proxmox-ve_9.1-1.iso")
        assert b.cache_key(a) != b.cache_key(c)

    def test_changes_with_static_ip(self) -> None:
        """Two VMs declaring different static IPs produce different
        installed systems (different ``/etc/network/interfaces``
        contents) — the cache MUST split or one build would shadow
        the other."""
        b = ProxmoxAnswerBuilder()
        a = _proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("OuterNet", ip="10.0.0.10"),
        ])
        c = _proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("OuterNet", ip="10.0.0.20"),
        ])
        assert b.cache_key(a) != b.cache_key(c)

    def test_changes_with_network_name(self) -> None:
        b = ProxmoxAnswerBuilder()
        a = _proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("OuterNet", ip="10.0.0.10"),
        ])
        c = _proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("MgmtNet", ip="10.0.0.10"),
        ])
        # Network name doesn't change the [network] block (filter is
        # interface-name-based, not net-name) — keys MAY match.  This
        # test exists to pin the current contract; if it ever breaks
        # from a [network] formula change, the implication for cache
        # locality is worth a deliberate decision.
        assert b.cache_key(a) == b.cache_key(c)



# ----------------------------------------------------------------------
# build_answer_toml — the TOML emitter is the most important surface;
# upstream PVE rejects anything that doesn't match its kebab-case schema.
# ----------------------------------------------------------------------


class TestBuildAnswerTomlGlobalBlock:
    def test_minimal_required_fields_present(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        for required in (
            "[global]",
            "country = ",
            "keyboard = ",
            "timezone = ",
            "fqdn = ",
            "mailto = ",
            "root-password = ",
            'reboot-mode = "power-off"',
        ):
            assert required in toml, f"missing {required!r} in:\n{toml}"

    def test_field_names_use_kebab_case(self) -> None:
        """PVE 9.x rejects underscored multi-word keys.  Verified
        against ``minimal.toml`` upstream test fixture."""
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        assert "root-password" in toml
        assert "root_password" not in toml

    def test_root_password_from_credential(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(
            _proxmox_vm(users=[Credential("root", "hunter2")]),
        )
        assert 'root-password = "hunter2"' in toml

    def test_ssh_keys_emitted_when_present(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(
            _proxmox_vm(users=[
                Credential("root", "p", ssh_key="ssh-ed25519 KEY1 a@b"),
            ]),
        )
        assert "root-ssh-keys" in toml
        assert '"ssh-ed25519 KEY1 a@b"' in toml

    def test_ssh_keys_omitted_when_absent(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(
            _proxmox_vm(users=[Credential("root", "p")]),
        )
        assert "root-ssh-keys" not in toml

    def test_fqdn_combines_vm_name_and_domain(self) -> None:
        toml = ProxmoxAnswerBuilder(
            fqdn_domain="example.test",
        ).build_answer_toml(_proxmox_vm(name="pve1"))
        assert 'fqdn = "pve1.example.test"' in toml

    def test_reboot_mode_is_power_off(self) -> None:
        """``reboot-mode = "power-off"`` is non-negotiable: the
        install-phase SHUTOFF wait-loop relies on the installer
        cleanly powering off (not rebooting into the cached system),
        and the default ``reboot`` mode would put the VM into a
        loop that can only be broken by the build timeout.
        """
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        assert 'reboot-mode = "power-off"' in toml


class TestBuildAnswerTomlNetworkBlock:
    """``[network]`` is the source of the worst PVE foot-guns —
    ``from-dhcp`` freezing the install-phase lease as static, the
    MAC filter not matching during install — so it deserves its own
    test class."""

    def test_static_mode_when_ip_declared(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        assert 'source = "from-answer"' in toml
        assert 'cidr = "10.0.0.10/24"' in toml
        assert 'gateway = "10.0.0.1"' in toml
        assert 'dns = "10.0.0.1"' in toml
        # Interface-name filter is stable across the install-to-run
        # MAC change.
        assert 'filter.ID_NET_NAME = "enp1s0"' in toml

    def test_gateway_is_first_host_of_subnet(self) -> None:
        b = ProxmoxAnswerBuilder()
        toml = b.build_answer_toml(_proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("OuterNet", ip="172.16.5.42"),
        ]))
        assert 'cidr = "172.16.5.42/24"' in toml
        assert 'gateway = "172.16.5.1"' in toml

    def test_explicit_gateway_overrides_derivation(self) -> None:
        toml = ProxmoxAnswerBuilder(
            network_gateway="10.0.0.254",
        ).build_answer_toml(_proxmox_vm())
        assert 'gateway = "10.0.0.254"' in toml
        # DNS still defaults to the resolved gateway.
        assert 'dns = "10.0.0.254"' in toml

    def test_explicit_dns_independent_of_gateway(self) -> None:
        toml = ProxmoxAnswerBuilder(
            network_dns="1.1.1.1",
        ).build_answer_toml(_proxmox_vm())
        assert 'gateway = "10.0.0.1"' in toml
        assert 'dns = "1.1.1.1"' in toml

    def test_custom_prefix(self) -> None:
        toml = ProxmoxAnswerBuilder(
            network_cidr_prefix=16,
        ).build_answer_toml(_proxmox_vm())
        assert 'cidr = "10.0.0.10/16"' in toml
        # First host of /16 is .0.1.
        assert 'gateway = "10.0.0.1"' in toml

    def test_custom_interface_filter(self) -> None:
        toml = ProxmoxAnswerBuilder(
            network_interface="ens18",
        ).build_answer_toml(_proxmox_vm())
        assert 'filter.ID_NET_NAME = "ens18"' in toml

    def test_dhcp_fallback_when_no_static_ip(self) -> None:
        # ``caplog`` is unreliable here: other tests in the suite call
        # ``testrange._logging.configure_root_logger``, which sets
        # ``propagate=False`` on the ``testrange`` logger.  Once that's
        # happened, caplog's root-level handler misses our records.
        # Pattern matches :mod:`tests.test_teardown_resilience` —
        # attach a handler directly to the module's logger.
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        target = logging.getLogger("testrange.vms.builders.proxmox_answer")
        prior_level = target.level
        target.addHandler(handler)
        target.setLevel(logging.WARNING)
        try:
            toml = ProxmoxAnswerBuilder().build_answer_toml(
                _proxmox_vm(devices=[
                    vCPU(2), Memory(4), HardDrive(64),
                    vNIC("OuterNet"),
                ]),
            )
        finally:
            target.removeHandler(handler)
            target.setLevel(prior_level)

        assert 'source = "from-dhcp"' in toml
        assert "from-answer" not in toml
        # Warning is the user-visible signal that they're in the
        # buggy mode and should set ``ip=...``.
        assert any(
            "from-dhcp" in r.getMessage() and "unreachable" in r.getMessage()
            for r in records
        ), f"warning not emitted; got records: {[r.getMessage() for r in records]!r}"

    def test_dhcp_fallback_when_no_network_ref(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(
            _proxmox_vm(devices=[vCPU(2), Memory(4), HardDrive(64)]),
        )
        assert 'source = "from-dhcp"' in toml


class TestBuildAnswerTomlDiskBlock:
    def test_default_filesystem_and_disk(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        assert "[disk-setup]" in toml
        assert 'filesystem = "ext4"' in toml
        assert 'disk-list = ["vda"]' in toml

    def test_custom_filesystem(self) -> None:
        toml = ProxmoxAnswerBuilder(
            filesystem="xfs",
        ).build_answer_toml(_proxmox_vm())
        assert 'filesystem = "xfs"' in toml

    def test_custom_disk_device(self) -> None:
        toml = ProxmoxAnswerBuilder(
            disk_device="sda",
        ).build_answer_toml(_proxmox_vm())
        assert 'disk-list = ["sda"]' in toml


class TestBuildAnswerTomlValidation:
    def test_no_root_credential_raises(self) -> None:
        with pytest.raises(CloudInitError, match="root_password"):
            ProxmoxAnswerBuilder().build_answer_toml(
                _proxmox_vm(users=[Credential("alice", "pw")]),
            )


# ----------------------------------------------------------------------
# post_install_hook — bake bootstrap (apt install dnsmasq + repo swap)
# into the cached install artifact so the run-phase network's internet
# state doesn't matter.
# ----------------------------------------------------------------------


class TestProxmoxAnswerHasPostInstallHook:
    def test_returns_true(self) -> None:
        # The bootstrap MUST run for the cached PVE template to work
        # on airgapped run-phase networks.  ``False`` here would
        # silently skip the hook and reintroduce the original bug.
        assert ProxmoxAnswerBuilder().has_post_install_hook() is True


class TestProxmoxAnswerPostInstallHook:
    def test_runs_pve_bootstrap_script_over_communicator(self) -> None:
        from testrange.vms.builders.proxmox_answer import _PVE_BOOTSTRAP_SCRIPT

        comm = MagicMock()
        comm.exec.return_value = MagicMock(exit_code=0, stderr=b"", stdout=b"")
        ProxmoxAnswerBuilder().post_install_hook(_proxmox_vm(), comm)
        comm.exec.assert_called_once()
        argv = comm.exec.call_args.args[0]
        assert argv == ["bash", "-c", _PVE_BOOTSTRAP_SCRIPT]
        # Generous timeout — apt-get update against the public PVE
        # mirror can take a minute on a cold node.
        assert comm.exec.call_args.kwargs.get("timeout") == 300

    def test_raises_when_bootstrap_exits_nonzero(self) -> None:
        comm = MagicMock()
        comm.exec.return_value = MagicMock(
            exit_code=100,
            stderr=b"E: Could not get lock /var/lib/dpkg/lock",
            stdout=b"",
        )
        with pytest.raises(CloudInitError, match="bootstrap"):
            ProxmoxAnswerBuilder().post_install_hook(_proxmox_vm(), comm)


class TestProxmoxAnswerPostInstallCacheKeyExtra:
    def test_returns_24_hex_chars(self) -> None:
        extra = ProxmoxAnswerBuilder().post_install_cache_key_extra(_proxmox_vm())
        assert len(extra) == 24
        assert all(c in "0123456789abcdef" for c in extra)

    def test_deterministic(self) -> None:
        a = ProxmoxAnswerBuilder().post_install_cache_key_extra(_proxmox_vm())
        b = ProxmoxAnswerBuilder().post_install_cache_key_extra(_proxmox_vm())
        assert a == b

    def test_changes_when_script_changes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bumping the script body must invalidate cached templates.
        # Otherwise an old cached PVE template would silently survive
        # a fix to the bootstrap and the airgapped-internet bug
        # would persist.
        from testrange.vms.builders import proxmox_answer as pa_mod

        before = ProxmoxAnswerBuilder().post_install_cache_key_extra(_proxmox_vm())
        monkeypatch.setattr(pa_mod, "_PVE_BOOTSTRAP_SCRIPT", "echo edited\n")
        after = ProxmoxAnswerBuilder().post_install_cache_key_extra(_proxmox_vm())
        assert before != after

    def test_folded_into_cache_key(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The whole point of the extra: cache_key changes when the
        # script changes, even with an otherwise-identical VM spec.
        from testrange.vms.builders import proxmox_answer as pa_mod

        b = ProxmoxAnswerBuilder()
        vm = _proxmox_vm()
        before = b.cache_key(vm)
        monkeypatch.setattr(pa_mod, "_PVE_BOOTSTRAP_SCRIPT", "echo edited\n")
        after = b.cache_key(vm)
        assert before != after


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


class TestPrimaryNetworkRef:
    def test_returns_first_virtual_network_ref(self) -> None:
        vm = _proxmox_vm(devices=[
            vCPU(2),
            vNIC("First", ip="1.1.1.1"),
            vNIC("Second", ip="2.2.2.2"),
        ])
        ref = _primary_network_ref(vm)
        assert ref is not None
        assert ref.ref == "First"

    def test_returns_none_when_no_network_ref(self) -> None:
        vm = _proxmox_vm(devices=[vCPU(2), Memory(4), HardDrive(64)])
        assert _primary_network_ref(vm) is None

    def test_skips_non_network_devices(self) -> None:
        vm = _proxmox_vm(devices=[
            vCPU(2), Memory(4), HardDrive(64),
            vNIC("Net", ip="10.0.0.5"),
        ])
        ref = _primary_network_ref(vm)
        assert ref is not None
        assert ref.ref == "Net"


class TestRootCredential:
    def test_returns_root_user(self) -> None:
        users = [Credential("alice", "p"), Credential("root", "rp")]
        assert _root_credential(users).username == "root"

    def test_raises_when_root_missing(self) -> None:
        with pytest.raises(CloudInitError, match="root_password"):
            _root_credential([Credential("alice", "p")])


class TestTomlStr:
    def test_plain_string_quoted(self) -> None:
        assert _toml_str("hello") == '"hello"'

    def test_double_quote_escaped(self) -> None:
        assert _toml_str('say "hi"') == '"say \\"hi\\""'

    def test_backslash_escaped(self) -> None:
        assert _toml_str(r"a\b") == r'"a\\b"'

    def test_newline_escaped_to_literal(self) -> None:
        # PVE's TOML parser doesn't accept basic strings with embedded
        # raw newlines.  Escape sequence is the safe representation.
        assert _toml_str("a\nb") == r'"a\nb"'


# ----------------------------------------------------------------------
# build_proxmox_seed_iso_bytes — pycdlib delegation, mirroring
# tests/test_unattend.py's TestBuildAutounattendIsoBytes.
# ----------------------------------------------------------------------


class TestBuildProxmoxSeedIsoBytes:
    def test_writes_one_file_with_proxmox_ais_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Volume label MUST be ``PROXMOX-AIS`` (or whatever the
        prepared installer's ``auto-installer-mode.toml`` told it to
        look for) — that's the udev label libvirt-attached seed
        ISOs are matched against by ``proxmox-fetch-answer``."""
        # ``PyCdlib`` is imported at module load time, so we patch
        # the bound name on the proxmox_answer module — same pattern
        # cloud_init / unattend tests use for their pycdlib calls.
        import testrange.vms.builders.proxmox_answer as pa

        iso_obj = MagicMock()
        monkeypatch.setattr(pa, "PyCdlib", lambda: iso_obj)

        build_proxmox_seed_iso_bytes("mode = \"partition\"\n")

        new_kwargs = iso_obj.new.call_args.kwargs
        assert new_kwargs["vol_ident"] == "PROXMOX-AIS"
        assert iso_obj.add_fp.call_count == 1
        add_kwargs = iso_obj.add_fp.call_args.kwargs
        assert add_kwargs["joliet_path"] == "/answer.toml"
        assert add_kwargs["iso_path"] == "/ANSWER.TOM;1"
        iso_obj.write_fp.assert_called_once()

    def test_custom_volume_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import testrange.vms.builders.proxmox_answer as pa

        iso_obj = MagicMock()
        monkeypatch.setattr(pa, "PyCdlib", lambda: iso_obj)

        build_proxmox_seed_iso_bytes("body", volume_label="OTHER-LABEL")
        assert iso_obj.new.call_args.kwargs["vol_ident"] == "OTHER-LABEL"

    def test_wraps_pycdlib_errors_in_cloud_init_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import testrange.vms.builders.proxmox_answer as pa

        class BadIso:
            def new(self, **_: Any) -> None: ...
            def add_fp(self, *_: Any, **__: Any) -> None:
                raise RuntimeError("boom")
            def close(self) -> None: ...

        monkeypatch.setattr(pa, "PyCdlib", lambda: BadIso())

        with pytest.raises(CloudInitError, match="boom"):
            build_proxmox_seed_iso_bytes("body")

    def test_seed_iso_carries_only_answer_toml(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The seed ISO must NOT carry a first-boot script — PVE's
        ``proxmox-fetch-answer`` reads it from
        ``/proxmox-first-boot`` on the *prepared installer ISO*
        instead.  An earlier slice incorrectly embedded the script
        on the seed ISO; PVE then aborted the install with "Failed
        loading first-boot executable from iso (was iso prepared
        with --on-first-boot)".  Pin the contract so the script
        never sneaks back onto the wrong ISO."""
        import testrange.vms.builders.proxmox_answer as pa

        iso_obj = MagicMock()
        monkeypatch.setattr(pa, "PyCdlib", lambda: iso_obj)

        build_proxmox_seed_iso_bytes("body")

        # Exactly one add_fp call: answer.toml.  No first-boot, no
        # other payloads.
        assert iso_obj.add_fp.call_count == 1
        joliet_paths = {
            call.kwargs["joliet_path"]
            for call in iso_obj.add_fp.call_args_list
        }
        assert joliet_paths == {"/answer.toml"}


# ----------------------------------------------------------------------
# Answer.toml stays minimal — no [first-boot] or other post-install
# section.  PVE installer doesn't have a generic post-install hook
# in answer.toml; for the dnsmasq-on-Hypervisor case TestRange runs
# the bootstrap over SSH from
# ProxmoxAnswerBuilder.post_install_hook (which the orchestrator
# fires by re-booting the install VM on the install network between
# SHUTOFF and template promotion) instead of embedding a script in
# the prepared installer ISO.
# ----------------------------------------------------------------------


class TestAnswerTomlOmitsFirstBoot:
    """Pin that the answer.toml generator never emits a
    ``[first-boot]`` section, regardless of ``vm.pkgs`` /
    ``vm.post_install_cmds``.  An earlier slice tried to render a
    first-boot script via answer.toml ``source = "from-iso"`` mode
    — that needed a script embedded in the prepared installer ISO
    via xorriso, plus cache-key invalidation across two layers, plus
    a chmod-via-Rock-Ridge dance to make the embedded file
    executable.  All ~300 lines deleted in favour of an SSH-side
    bootstrap; this test guards against the section coming back."""

    def test_no_first_boot_with_no_pkgs(self) -> None:
        toml = ProxmoxAnswerBuilder().build_answer_toml(_proxmox_vm())
        assert "[first-boot]" not in toml

    def test_no_first_boot_with_pkgs(self) -> None:
        from testrange.packages import Apt
        vm = _proxmox_vm(pkgs=[Apt("dnsmasq"), Apt("tmux")])
        toml = ProxmoxAnswerBuilder().build_answer_toml(vm)
        assert "[first-boot]" not in toml
        # And the package names don't accidentally leak into the
        # answer-toml as some misguided `[packages]` section.
        assert "dnsmasq" not in toml
        assert "tmux" not in toml


# ----------------------------------------------------------------------
# prepare_install_domain / prepare_run_domain — the InstallDomain /
# RunDomain dataclasses are what the libvirt backend consumes;
# regressions here would silently change install behaviour.
# ----------------------------------------------------------------------


class TestPrepareInstallDomain:
    def test_install_domain_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        vm = _proxmox_vm()

        cache = MagicMock()
        cache.get_proxmox_prepared_iso.return_value = tmp_path / "prepared.iso"
        cache.stage_source.side_effect = lambda p, _b: str(p)

        # Avoid the real pycdlib seed-ISO write.
        import testrange.vms.builders.proxmox_answer as pa
        monkeypatch.setattr(
            pa, "build_proxmox_seed_iso_bytes",
            lambda _toml, **_kw: b"seed",
        )
        monkeypatch.setattr(
            pa, "resolve_image", lambda iso, _cache: Path("/cache/" + iso.split("/")[-1]),
        )

        spec = ProxmoxAnswerBuilder().prepare_install_domain(vm, run, cache)

        assert isinstance(spec, InstallDomain)
        assert spec.uefi is True
        assert spec.windows is False
        assert spec.boot_cdrom is True
        assert spec.seed_iso is not None
        assert len(spec.extra_cdroms) == 1
        # work_disk lives under the run dir as a fresh blank qcow2.
        assert Path(spec.work_disk).parent == Path(run.path)
        run.cleanup()

    def test_uefi_false_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flipping ``uefi=False`` on the builder must propagate to
        the InstallDomain so callers don't accidentally ship UEFI
        when they explicitly opted into BIOS."""
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        cache = MagicMock()
        cache.get_proxmox_prepared_iso.return_value = tmp_path / "p.iso"
        cache.stage_source.side_effect = lambda p, _b: str(p)
        import testrange.vms.builders.proxmox_answer as pa
        monkeypatch.setattr(
            pa, "build_proxmox_seed_iso_bytes",
            lambda _toml, **_kw: b"seed",
        )
        monkeypatch.setattr(
            pa, "resolve_image", lambda iso, _c: Path("/c/" + iso.split("/")[-1]),
        )

        spec = ProxmoxAnswerBuilder(uefi=False).prepare_install_domain(
            _proxmox_vm(), run, cache,
        )
        assert spec.uefi is False
        run.cleanup()

    def test_get_proxmox_prepared_iso_called_with_only_vanilla(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The prepared-ISO call must NOT carry a first-boot script
        kwarg.  Earlier slices threaded a rendered script through
        ``cache.get_proxmox_prepared_iso(vanilla,
        first_boot_script=...)`` to embed it on the prepared ISO via
        xorriso ``--on-first-boot``; that machinery is gone (the
        dnsmasq bootstrap runs over SSH from
        ``ProxmoxAnswerBuilder.post_install_hook`` during the
        install phase instead).  Regression guard against putting
        the kwarg back."""
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend
        from testrange.packages import Apt

        run = RunDir(LocalStorageBackend(tmp_path))
        cache = MagicMock()
        cache.get_proxmox_prepared_iso.return_value = tmp_path / "p.iso"
        cache.stage_source.side_effect = lambda p, _b: str(p)

        import testrange.vms.builders.proxmox_answer as pa
        monkeypatch.setattr(
            pa, "build_proxmox_seed_iso_bytes",
            lambda _toml, **_kw: b"seed",
        )
        monkeypatch.setattr(
            pa, "resolve_image", lambda iso, _c: Path("/c/" + iso.split("/")[-1]),
        )

        vm = _proxmox_vm(pkgs=[Apt("dnsmasq")])
        ProxmoxAnswerBuilder().prepare_install_domain(vm, run, cache)

        call = cache.get_proxmox_prepared_iso.call_args
        assert "first_boot_script" not in call.kwargs
        run.cleanup()


class TestPrepareRunDomain:
    def test_run_domain_no_seed_uefi_no_windows(
        self, tmp_path: Path,
    ) -> None:
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        spec = ProxmoxAnswerBuilder().prepare_run_domain(
            _proxmox_vm(), run, mac_ip_pairs=[],
        )
        assert isinstance(spec, RunDomain)
        # No phase-2 seed: PVE doesn't have cloud-init's instance-id
        # rotation requirement, and the static [network] config is
        # already baked into the cached qcow2.
        assert spec.seed_iso is None
        assert spec.uefi is True
        assert spec.windows is False
        run.cleanup()

    def test_uefi_propagates_from_builder(self, tmp_path: Path) -> None:
        """Run-phase firmware family MUST match what install used —
        booting a UEFI-installed disk under SeaBIOS panics
        immediately because the EFI partition isn't bootable in BIOS
        mode.  This test pins the propagation."""
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        spec = ProxmoxAnswerBuilder(uefi=False).prepare_run_domain(
            _proxmox_vm(), run, mac_ip_pairs=[],
        )
        assert spec.uefi is False
        run.cleanup()


# ----------------------------------------------------------------------
# install_manifest — the JSON sidecar written next to the cached
# qcow2; humans inspect it to identify cache entries.
# ----------------------------------------------------------------------


class TestInstallManifest:
    def test_includes_proxmox_marker(self) -> None:
        m = ProxmoxAnswerBuilder().install_manifest(_proxmox_vm(), "abc123")
        assert m["proxmox"] is True
        assert m["config_hash"] == "abc123"

    def test_includes_user_summary(self) -> None:
        m = ProxmoxAnswerBuilder().install_manifest(
            _proxmox_vm(users=[
                Credential("root", "p"),
                Credential("ops", "p", sudo=True),
            ]),
            "h",
        )
        assert {"username": "root", "sudo": False} in m["users"]
        assert {"username": "ops", "sudo": True} in m["users"]

    def test_excludes_password_and_ssh_key(self) -> None:
        """The manifest is a debugging aid in clear-text JSON next
        to the cached disk — passwords and keys must not leak in."""
        # Use a long distinctive password / key so a substring match
        # against the manifest repr doesn't false-positive on
        # something else in there (e.g. the ISO URL).
        secret_pw = "Tr0ub4dor&3-MANIFEST-FIXTURE-PW"
        secret_key = "ssh-ed25519 MANIFEST-FIXTURE-PUBKEY-DATA"
        m = ProxmoxAnswerBuilder().install_manifest(
            _proxmox_vm(users=[
                Credential("root", secret_pw, ssh_key=secret_key),
            ]),
            "h",
        )
        text = repr(m)
        assert secret_pw not in text
        assert secret_key not in text
        assert "MANIFEST-FIXTURE" not in text
