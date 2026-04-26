"""Unit tests for :mod:`testrange.backends.libvirt.vm`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from testrange.backends.libvirt.vm import VM
from testrange.credentials import Credential
from testrange.devices import HardDrive, Memory, vNIC, vCPU
from testrange.packages import Apt


@pytest.fixture
def basic_vm() -> VM:
    return VM(
        name="web01",
        iso="/tmp/fake.qcow2",
        users=[Credential("root", "pw")],
        pkgs=[Apt("nginx")],
        devices=[vCPU(4), Memory(4), vNIC("NetA")],
    )


class TestConstruction:
    def test_defaults_for_optional_args(self) -> None:
        vm = VM(name="a", iso="b", users=[])
        assert vm.pkgs == []
        assert vm.post_install_cmds == []
        assert vm.devices == []

    def test_name_property_reflects_ctor(self, basic_vm: VM) -> None:
        # The `.name` property wraps `_name`; this codifies the public read path.
        assert basic_vm.name == "web01"


class TestDeviceAccessors:
    def test_vcpu_default(self) -> None:
        vm = VM("a", "b", [], devices=[])
        assert vm._vcpu_count() == 2

    def test_vcpu_from_device(self) -> None:
        vm = VM("a", "b", [], devices=[vCPU(8)])
        assert vm._vcpu_count() == 8

    def test_memory_default_in_kib(self) -> None:
        vm = VM("a", "b", [], devices=[])
        assert vm._memory_kib() == 2 * 1024 * 1024

    def test_memory_from_device(self) -> None:
        vm = VM("a", "b", [], devices=[Memory(8)])
        assert vm._memory_kib() == 8 * 1024 * 1024

    def test_hard_drives_filter(self) -> None:
        vm = VM("a", "b", [], devices=[HardDrive("32GB"), vCPU(), HardDrive("64GB")])
        assert len(vm._hard_drives()) == 2

    def test_network_refs_filter(self) -> None:
        vm = VM(
            "a", "b", [],
            devices=[vNIC("X"), vCPU(), vNIC("Y")],
        )
        refs = vm._network_refs()
        assert [r.ref for r in refs] == ["X", "Y"]

    def test_primary_disk_size_default(self) -> None:
        vm = VM("a", "b", [], devices=[])
        assert vm._primary_disk_size() == "20G"

    def test_primary_disk_size_from_first_drive(self) -> None:
        vm = VM("a", "b", [], devices=[HardDrive("64GB")])
        assert vm._primary_disk_size() == "64G"


class TestBuilderCacheKey:
    """Install-phase cache keys live on the builder now; VM no longer
    has its own ``config_hash()`` method.  These tests exercise the
    Linux (cloud-init) builder — identical invariants apply to the
    Windows one."""

    def test_hash_deterministic(self, basic_vm: VM) -> None:
        assert (
            basic_vm.builder.cache_key(basic_vm)
            == basic_vm.builder.cache_key(basic_vm)
        )

    def test_hash_length(self, basic_vm: VM) -> None:
        assert len(basic_vm.builder.cache_key(basic_vm)) == 24

    def test_hash_changes_with_iso(self) -> None:
        a = VM("vm", "debian-12", [Credential("root", "pw")])
        b = VM("vm", "debian-13", [Credential("root", "pw")])
        assert a.builder.cache_key(a) != b.builder.cache_key(b)

    def test_hash_ignores_ssh_keys(self) -> None:
        """Regression: SSH key rotation should not invalidate cache."""
        a = VM("vm", "x", [Credential("root", "pw", ssh_key="ssh-rsa A")])
        b = VM("vm", "x", [Credential("root", "pw", ssh_key="ssh-rsa B")])
        assert a.builder.cache_key(a) == b.builder.cache_key(b)


class TestDomainXml:
    def _mk_vm(self, **overrides) -> VM:
        return VM(
            name=overrides.pop("name", "web01"),
            iso=overrides.pop("iso", "/tmp/x.qcow2"),
            users=overrides.pop("users", []),
            devices=overrides.pop("devices", [vCPU(2), Memory(2)]),
        )

    def test_xml_well_formed(self) -> None:
        vm = self._mk_vm()
        xml = vm._base_domain_xml(
            "tr-web01-abcd1234",
            disk_path=Path("/tmp/disk.qcow2"),
            seed_iso_path=Path("/tmp/seed.iso"),
            network_entries=[("tr-neta-abcd", "52:54:00:01:02:03")],
            run_id="abcd1234",
        )
        ET.fromstring(xml)

    def test_vcpu_and_memory_in_xml(self) -> None:
        vm = VM("a", "b", [], devices=[vCPU(8), Memory(16)])
        xml = vm._base_domain_xml(
            "name", Path("/t/d.qcow2"), Path("/t/s.iso"), [], "r",
        )
        root = ET.fromstring(xml)
        vcpu = root.find("vcpu")
        mem = root.find("memory")
        assert vcpu is not None and mem is not None
        assert vcpu.text == "8"
        assert mem.text is not None and int(mem.text) == 16 * 1024 * 1024

    def test_rejects_foreign_backend_device_at_runtime(self) -> None:
        """The pyright union type catches cross-backend devices at
        edit time; this regression covers the runtime path for
        callers who bypass the type checker (dynamic test factories,
        YAML loaders, plain `# type: ignore`)."""
        from testrange.devices import AbstractHardDrive

        class ProxmoxHardDrive(AbstractHardDrive):
            def __init__(self, size: int) -> None:
                self.size = f"{size}GiB"

        from testrange.exceptions import VMBuildError as _VMBuildError
        with pytest.raises(_VMBuildError, match="not accepted by the libvirt backend"):
            VM(
                "x", "y.qcow2", [],
                devices=[ProxmoxHardDrive(10)],  # type: ignore[list-item]
            )

    def test_nvme_disk_uses_nvme_bus(self) -> None:
        from testrange.backends.libvirt import LibvirtHardDrive
        vm = VM("a", "b", [], devices=[LibvirtHardDrive("20GB", nvme=True)])
        xml = vm._base_domain_xml(
            "n", Path("/d.qcow2"), Path("/s.iso"), [], "r",
        )
        root = ET.fromstring(xml)
        primary_disk = root.find(".//devices/disk[@device='disk']")
        assert primary_disk is not None
        target = primary_disk.find("target")
        assert target is not None
        assert target.attrib["bus"] == "nvme"

    def test_virtio_disk_default(self) -> None:
        vm = VM("a", "b", [], devices=[HardDrive()])
        xml = vm._base_domain_xml("n", Path("/d.qcow2"), Path("/s.iso"), [], "r")
        root = ET.fromstring(xml)
        primary_disk = root.find(".//devices/disk[@device='disk']")
        assert primary_disk is not None
        target = primary_disk.find("target")
        assert target is not None
        assert target.attrib["bus"] == "virtio"

    def test_multiple_nics_emitted(self) -> None:
        vm = VM("a", "b", [])
        xml = vm._base_domain_xml(
            "n", Path("/d.qcow2"), Path("/s.iso"),
            [("net1", "52:54:00:01:02:03"), ("net2", "52:54:00:01:02:04")],
            "r",
        )
        root = ET.fromstring(xml)
        ifaces = root.findall(".//interface")
        assert len(ifaces) == 2

    def test_guest_agent_channel_present(self) -> None:
        vm = VM("a", "b", [])
        xml = vm._base_domain_xml("n", Path("/d.qcow2"), Path("/s.iso"), [], "r")
        assert "org.qemu.guest_agent.0" in xml

    def test_no_kernel_cmdline_element(self) -> None:
        # qemu rejects -append without -kernel when booting from disk.
        # NoCloud detection relies on the "cidata" volume label on the seed
        # ISO, not on a kernel cmdline argument.
        vm = VM("a", "b", [])
        xml = vm._base_domain_xml("n", Path("/d.qcow2"), Path("/s.iso"), [], "r")
        assert "<cmdline>" not in xml


class TestRepr:
    def test_repr(self, basic_vm: VM) -> None:
        r = repr(basic_vm)
        assert "web01" in r
        assert "/tmp/fake.qcow2" in r


class TestVncDebugToggle:
    """``TESTRANGE_VNC=1`` attaches a VNC graphics device + QXL video so
    ``virt-viewer`` can show the installer screen.  Off by default —
    CI / headless runs should not surface a listener."""

    def _render(self, basic_vm: VM) -> ET.Element:
        xml = basic_vm._base_domain_xml(
            domain_name="tr-dbg",
            disk_path=Path("/tmp/d.qcow2"),
            seed_iso_path=None,
            network_entries=[],
            run_id="deadbeef",
        )
        return ET.fromstring(xml)

    def test_no_graphics_by_default(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("TESTRANGE_VNC", raising=False)
        root = self._render(basic_vm)
        assert root.find(".//graphics") is None
        assert root.find(".//video") is None

    def test_vnc_enabled_by_env(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TESTRANGE_VNC", "1")
        root = self._render(basic_vm)
        gfx = root.find(".//graphics")
        assert gfx is not None
        assert gfx.get("type") == "vnc"
        assert gfx.get("listen") == "127.0.0.1"
        assert gfx.get("autoport") == "yes"
        video = root.find(".//video/model")
        assert video is not None
        assert video.get("type") == "qxl"


class TestShutdown:
    def test_shutdown_without_start_raises(self, basic_vm: VM) -> None:
        from testrange.exceptions import VMNotRunningError

        with pytest.raises(VMNotRunningError):
            basic_vm.shutdown()

    def test_shutdown_clears_state(self, basic_vm: VM) -> None:
        basic_vm._domain = MagicMock()
        basic_vm._communicator = MagicMock()
        basic_vm.shutdown()
        assert basic_vm._domain is None
        assert basic_vm._communicator is None

    def test_shutdown_cleans_up_install_domain(self, basic_vm: VM) -> None:
        """Safety net: if the install-phase ``finally`` failed to clean
        up (e.g. libvirt dropped), orchestrator teardown calls
        ``shutdown()`` which must still destroy+undefine the stashed
        install domain."""
        install_dom = MagicMock()
        install_dom.isActive.return_value = True
        basic_vm._install_domain = install_dom

        basic_vm.shutdown()

        install_dom.destroy.assert_called_once()
        install_dom.undefineFlags.assert_called_once()
        assert basic_vm._install_domain is None

    def test_shutdown_cleans_both_domains(self, basic_vm: VM) -> None:
        install_dom = MagicMock()
        install_dom.isActive.return_value = False
        run_dom = MagicMock()
        run_dom.isActive.return_value = True
        basic_vm._install_domain = install_dom
        basic_vm._domain = run_dom

        basic_vm.shutdown()

        install_dom.undefineFlags.assert_called_once()
        run_dom.destroy.assert_called_once()
        run_dom.undefineFlags.assert_called_once()
        assert basic_vm._install_domain is None
        assert basic_vm._domain is None


class TestInstallPhaseCleanup:
    """Bug #1 regression: the install-phase domain must always be
    destroyed + undefined, even on KeyboardInterrupt mid-wait or any
    other exception in the polling loop.  Without the ``try/finally``
    wrapping the wait, a Ctrl+C during a 30-minute Windows install
    leaves an orphaned ``tr-build-*`` VM under ``qemu:///system`` with
    no Python process to tidy it."""

    def _patch_install(
        self, vm: VM, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
        """Stub out enough of the install-phase pipeline that
        ``_run_install_phase`` runs against pure mocks."""
        from testrange._run import RunDir
        from testrange.cache import CacheManager
        from testrange.vms.builders.base import InstallDomain

        dom = MagicMock()
        dom.isActive.return_value = True
        conn = MagicMock()
        conn.defineXML.return_value = dom

        cache = MagicMock(spec=CacheManager)
        cache.store_vm.return_value = Path("/cache/winbox.qcow2")

        run = MagicMock(spec=RunDir)
        run.run_id = "deadbeef-1111-2222-3333-444455556666"
        run.path_for.return_value = "/tmp/vars.fd"

        install_spec = InstallDomain(
            work_disk="/tmp/winbox-install.qcow2",
            seed_iso="/tmp/winbox-unattend.iso",
            extra_cdroms=(),
            uefi=False,
            windows=False,
            boot_cdrom=False,
        )
        monkeypatch.setattr(
            vm.builder, "prepare_install_domain",
            lambda _vm, _run, _cache: install_spec,
        )
        monkeypatch.setattr(
            vm.builder, "install_manifest",
            lambda _vm, _h: {"name": _vm.name},
        )
        monkeypatch.setattr(
            "testrange.backends.libvirt.vm._POLL_INTERVAL", 0,
        )

        return dom, conn, cache, run

    def test_keyboardinterrupt_mid_wait_still_cleans_up(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        # First poll raises KeyboardInterrupt — simulate user Ctrl+C.
        dom.state.side_effect = KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            basic_vm._run_install_phase(
                conn=conn, cache=cache, run=run,
                install_network_name="tr-i",
                install_network_mac="52:54:00:00:00:01",
                h="cafebabe",
            )

        dom.destroy.assert_called_once()
        dom.undefineFlags.assert_called_once()
        assert basic_vm._install_domain is None

    def test_build_error_mid_wait_still_cleans_up(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from testrange.exceptions import VMBuildError
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        dom.state.side_effect = VMBuildError("simulated bug")

        with pytest.raises(VMBuildError):
            basic_vm._run_install_phase(
                conn=conn, cache=cache, run=run,
                install_network_name="tr-i",
                install_network_mac="52:54:00:00:00:01",
                h="cafebabe",
            )

        dom.destroy.assert_called_once()
        dom.undefineFlags.assert_called_once()
        assert basic_vm._install_domain is None

    def test_successful_shutoff_also_cleans_up(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: shutoff detected, cache write succeeds, domain
        still gets destroyed+undefined by the finally block."""
        import libvirt
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        dom.isActive.return_value = False  # already shutoff
        dom.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)

        result = basic_vm._run_install_phase(
            conn=conn, cache=cache, run=run,
            install_network_name="tr-i",
            install_network_mac="52:54:00:00:00:01",
            h="cafebabe",
        )
        assert result == Path("/cache/winbox.qcow2")
        dom.undefineFlags.assert_called_once()
        dom.destroy.assert_not_called()  # not active → no destroy
        assert basic_vm._install_domain is None

    def test_create_failure_undefines_the_defined_domain(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``defineXML`` persists a domain entry.  If the subsequent
        ``create()`` raises, the entry must be undefined so the next
        run doesn't inherit an orphan ``tr-build-*`` in libvirt."""
        import libvirt
        from testrange.exceptions import VMBuildError
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        dom.isActive.return_value = False  # never actually started
        dom.create.side_effect = libvirt.libvirtError("create failed")

        with pytest.raises(VMBuildError, match="Failed to start install domain"):
            basic_vm._run_install_phase(
                conn=conn, cache=cache, run=run,
                install_network_name="tr-i",
                install_network_mac="52:54:00:00:00:01",
                h="cafebabe",
            )

        # The defined-but-never-started domain must be undefined.
        dom.undefineFlags.assert_called_once()
        # Not active → destroy shouldn't be called.
        dom.destroy.assert_not_called()
        assert basic_vm._install_domain is None

    def test_persistent_state_errors_bail_early_with_diagnostic(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If ``domain.state()`` raises consistently (libvirtd died),
        the install-phase poll must surface the real error fast —
        not silently eat errors until _BUILD_TIMEOUT."""
        import libvirt
        from testrange.exceptions import VMBuildError
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        dom.state.side_effect = libvirt.libvirtError("connection lost")

        with pytest.raises(
            VMBuildError, match="Lost libvirt connection",
        ) as excinfo:
            basic_vm._run_install_phase(
                conn=conn, cache=cache, run=run,
                install_network_name="tr-i",
                install_network_mac="52:54:00:00:00:01",
                h="cafebabe",
            )

        # Called exactly the cap number of times (5).  The cap is what
        # keeps us from silently sitting through a 30-minute timeout.
        from testrange.backends.libvirt import vm as vm_mod
        assert dom.state.call_count == vm_mod._MAX_CONSECUTIVE_STATE_ERRORS
        assert "connection lost" in str(excinfo.value)

        # Cleanup still ran — the finally block is unconditional.
        dom.undefineFlags.assert_called_once()
        assert basic_vm._install_domain is None

    def test_transient_state_error_does_not_bail(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A one-off libvirtError followed by a successful state() must
        not count against the error cap — counter resets on success."""
        import libvirt
        dom, conn, cache, run = self._patch_install(basic_vm, monkeypatch)
        dom.isActive.return_value = False

        # 1st call: transient error; 2nd call: success, SHUTOFF.
        dom.state.side_effect = [
            libvirt.libvirtError("blip"),
            (libvirt.VIR_DOMAIN_SHUTOFF, 0),
        ]

        result = basic_vm._run_install_phase(
            conn=conn, cache=cache, run=run,
            install_network_name="tr-i",
            install_network_mac="52:54:00:00:00:01",
            h="cafebabe",
        )
        assert result == Path("/cache/winbox.qcow2")
        assert dom.state.call_count == 2


class TestBootKeypressSpam:
    """Windows install ISOs under UEFI show a 'Press any key to boot
    from CD or DVD...' prompt that times out in headless VMs.  Builders
    that return ``needs_boot_keypress()=True`` trigger a short-lived
    thread that sends spacebars to the install domain during early
    boot."""

    def test_windows_builder_requests_keypress(self) -> None:
        from testrange.vms.builders import (
            CloudInitBuilder,
            NoOpBuilder,
            WindowsUnattendedBuilder,
        )
        assert WindowsUnattendedBuilder().needs_boot_keypress() is True
        assert CloudInitBuilder().needs_boot_keypress() is False
        assert NoOpBuilder().needs_boot_keypress() is False

    def test_keypress_thread_spawned_only_for_windows(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The install path must spawn a keypress thread iff the
        builder asks for it, and that thread must call ``sendKey`` with
        the Linux SPACE keycode (57)."""
        import libvirt

        from testrange.backends.libvirt import vm as vm_mod
        from testrange.vms.builders import WindowsUnattendedBuilder

        # Fast-forward the spam loop so the test runs in ms, not seconds.
        monkeypatch.setattr(vm_mod, "_BOOT_KEYPRESS_WINDOW_S", 0.3)
        monkeypatch.setattr(vm_mod, "_BOOT_KEYPRESS_INTERVAL_S", 0.05)

        win_vm = VM(
            name="winbox",
            iso="/srv/iso/Win10_21H1_English_x64.iso",
            users=[Credential("root", "Admin1!")],
            builder=WindowsUnattendedBuilder(),
        )
        helper = TestInstallPhaseCleanup()
        dom, conn, cache, run = helper._patch_install(win_vm, monkeypatch)
        dom.isActive.return_value = False
        dom.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)

        win_vm._run_install_phase(
            conn=conn, cache=cache, run=run,
            install_network_name="tr-i",
            install_network_mac="52:54:00:00:00:01",
            h="cafebabe",
        )

        # sendKey was called at least once with SPACE (keycode 57).
        assert dom.sendKey.called
        call = dom.sendKey.call_args
        # positional signature: (codeset, holdtime, keycodes, nkeycodes, flags)
        assert call.args[2] == [57]
        assert call.args[0] == libvirt.VIR_KEYCODE_SET_LINUX

    def test_linux_builder_does_not_spawn_keypress_thread(
        self, basic_vm: VM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import libvirt

        helper = TestInstallPhaseCleanup()
        dom, conn, cache, run = helper._patch_install(basic_vm, monkeypatch)
        dom.isActive.return_value = False
        dom.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)

        basic_vm._run_install_phase(
            conn=conn, cache=cache, run=run,
            install_network_name="tr-i",
            install_network_mac="52:54:00:00:00:01",
            h="cafebabe",
        )

        # No sendKey for cloud-init Linux builds — just wastes cycles
        # and risks hitting a GRUB menu prompt unintentionally.
        dom.sendKey.assert_not_called()


class TestBuildCacheLock:
    """Concurrency: two builds sharing a config hash must serialise so they
    don't both run the install phase and race on the cache write."""

    def test_cache_hit_skips_install(self, tmp_path: Path) -> None:
        from testrange._run import RunDir
        from testrange.cache import CacheManager

        vm = VM(
            name="hit-probe",
            iso="/tmp/fake.qcow2",
            users=[Credential("root", "pw")],
        )
        cache = MagicMock(spec=CacheManager)
        # Simulate a populated cache.
        cache.get_vm.return_value = tmp_path / "precached.qcow2"

        # If the build takes the fast path, _run_install_phase should
        # never be called — monkeypatch it to blow up if it is.
        vm._run_install_phase = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError(
                "_run_install_phase must not run on cache hit"
            )
        )

        conn = MagicMock()
        run = MagicMock(spec=RunDir)
        run.run_id = "deadbeef"

        result = vm.build(
            context=conn,
            cache=cache,
            run=run,
            install_network_name="tr-instal-x",
            install_network_mac="52:54:00:00:00:01",
        )
        assert result == tmp_path / "precached.qcow2"
        vm._run_install_phase.assert_not_called()

    def test_second_builder_with_same_hash_hits_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After one builder installs + stores, the next call sees the
        cache populated and skips the install phase.  This is the
        scenario the per-hash build lock enables under concurrency."""
        from testrange._run import RunDir
        from testrange.cache import CacheManager

        stored_path = tmp_path / "after-install.qcow2"

        cache = MagicMock(spec=CacheManager)
        # First invocation misses; second (post-install) hits.
        cache.get_vm.side_effect = [None, stored_path]

        vm = VM(
            name="racer",
            iso="/tmp/fake.qcow2",
            users=[Credential("root", "pw")],
        )
        install_called = {"count": 0}

        def _fake_install(**_kw) -> Path:
            install_called["count"] += 1
            return stored_path

        vm._run_install_phase = _fake_install  # type: ignore[method-assign]

        conn = MagicMock()
        run = MagicMock(spec=RunDir)
        run.run_id = "deadbeef"

        # First build: cache miss → installs.
        first = vm.build(
            context=conn, cache=cache, run=run,
            install_network_name="tr-i", install_network_mac="52:54:00:00:00:01",
        )
        assert first == stored_path
        assert install_called["count"] == 1

        # Second build with identical spec: cache hit, no install.
        second = vm.build(
            context=conn, cache=cache, run=run,
            install_network_name="tr-i", install_network_mac="52:54:00:00:00:01",
        )
        assert second == stored_path
        assert install_called["count"] == 1, (
            "second build must not re-run install — cache should be hit"
        )


class TestFileHelpers:
    def test_read_text_decodes_bytes(self, basic_vm: VM) -> None:
        basic_vm._communicator = MagicMock()
        basic_vm._communicator.get_file.return_value = "héllo".encode()
        assert basic_vm.read_text("/etc/motd") == "héllo"
        basic_vm._communicator.get_file.assert_called_once_with("/etc/motd")

    def test_read_text_custom_encoding(self, basic_vm: VM) -> None:
        basic_vm._communicator = MagicMock()
        basic_vm._communicator.get_file.return_value = "café".encode("latin-1")
        assert basic_vm.read_text("/f", encoding="latin-1") == "café"

    def test_write_text_encodes_before_put(self, basic_vm: VM) -> None:
        basic_vm._communicator = MagicMock()
        basic_vm.write_text("/tmp/greet", "hi\n")
        basic_vm._communicator.put_file.assert_called_once_with(
            "/tmp/greet", b"hi\n"
        )

    def test_download_writes_to_host_and_returns_path(
        self, basic_vm: VM, tmp_path: Path
    ) -> None:
        basic_vm._communicator = MagicMock()
        basic_vm._communicator.get_file.return_value = b"payload"
        dest = tmp_path / "out" / "file.bin"  # missing parent
        returned = basic_vm.download("/var/log/syslog", dest)
        assert returned == dest
        assert dest.read_bytes() == b"payload"

    def test_download_accepts_str_path(
        self, basic_vm: VM, tmp_path: Path
    ) -> None:
        basic_vm._communicator = MagicMock()
        basic_vm._communicator.get_file.return_value = b"x"
        dest = tmp_path / "a.bin"
        returned = basic_vm.download("/r", str(dest))
        assert returned == dest

    def test_upload_reads_host_and_sends(
        self, basic_vm: VM, tmp_path: Path
    ) -> None:
        basic_vm._communicator = MagicMock()
        src = tmp_path / "conf.txt"
        src.write_bytes(b"config-body")
        basic_vm.upload(src, "/etc/app.conf")
        basic_vm._communicator.put_file.assert_called_once_with(
            "/etc/app.conf", b"config-body"
        )

    def test_upload_missing_host_file_raises(
        self, basic_vm: VM, tmp_path: Path
    ) -> None:
        basic_vm._communicator = MagicMock()
        with pytest.raises(FileNotFoundError):
            basic_vm.upload(tmp_path / "does-not-exist", "/remote")
        basic_vm._communicator.put_file.assert_not_called()

    def test_all_helpers_require_running_vm(self, basic_vm: VM) -> None:
        from testrange.exceptions import VMNotRunningError

        with pytest.raises(VMNotRunningError):
            basic_vm.read_text("/etc/motd")
        with pytest.raises(VMNotRunningError):
            basic_vm.write_text("/tmp/x", "y")
        with pytest.raises(VMNotRunningError):
            basic_vm.download("/r", "/tmp/dest")
