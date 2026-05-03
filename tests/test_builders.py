"""Abstract :class:`Builder` interface + concrete-class behaviour tests.

Install-phase plumbing (domain XML, libvirt state machine) is covered
by the higher-level VM tests; this file exercises just the builder
contract: default_communicator, needs_install_phase, cache_key,
prepare_install_domain / prepare_run_domain output shapes, and the
NoOp-specific ``ready_image`` flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange import (
    VM,
    Builder,
    CloudInitBuilder,
    Credential,
    NoOpBuilder,
    WindowsUnattendedBuilder,
)
from testrange.vms.builders.base import InstallDomain, RunDomain


def _linux_vm(**overrides: Any) -> VM:
    # dict[str, Any] so pyright doesn't infer a narrow union from
    # the literal values and fail to reconcile with VM(**…) params.
    defaults: dict[str, Any] = dict(
        name="deb",
        iso="https://example.com/debian.qcow2",
        users=[Credential("root", "pw")],
    )
    defaults.update(overrides)
    return VM(**defaults)


def _windows_vm(**overrides: Any) -> VM:
    defaults: dict[str, Any] = dict(
        name="winbox",
        iso="/srv/iso/Win10_21H1_English_x64.iso",
        users=[
            Credential("root", "AdminPwd!"),
            Credential("deploy", "Deploy1!", sudo=True),
        ],
    )
    defaults.update(overrides)
    return VM(**defaults)


def _noop_vm(tmp_path: Path, *, windows: bool = False, name: str = "byoi") -> VM:
    src = tmp_path / f"{name}.qcow2"
    src.write_bytes(b"stub")
    return VM(
        name=name,
        iso=str(src),
        users=[Credential("deploy", "pw")],
        builder=NoOpBuilder(windows=windows),
    )


# ----------------------------------------------------------------------
# Common invariants that every concrete Builder must satisfy.
# ----------------------------------------------------------------------


class TestBuilderContract:
    """These assertions encode the shape of the :class:`Builder` ABC so
    subclasses can't silently drift.  One parametrized test per
    invariant."""

    @pytest.mark.parametrize("builder_cls", [
        CloudInitBuilder,
        WindowsUnattendedBuilder,
        NoOpBuilder,
    ])
    def test_builders_are_subclasses_of_abc(self, builder_cls) -> None:
        assert issubclass(builder_cls, Builder)
        assert isinstance(builder_cls(), Builder)

    @pytest.mark.parametrize("builder,expected", [
        (CloudInitBuilder(), "guest-agent"),
        (WindowsUnattendedBuilder(), "winrm"),
        (NoOpBuilder(windows=False), "guest-agent"),
        (NoOpBuilder(windows=True), "winrm"),
    ])
    def test_default_communicator(self, builder: Builder, expected: str) -> None:
        assert builder.default_communicator() == expected

    @pytest.mark.parametrize("builder,expected", [
        (CloudInitBuilder(), True),
        (WindowsUnattendedBuilder(), True),
        (NoOpBuilder(), False),
    ])
    def test_needs_install_phase(self, builder: Builder, expected: bool) -> None:
        assert builder.needs_install_phase() is expected

    @pytest.mark.parametrize("builder", [
        CloudInitBuilder(),
        WindowsUnattendedBuilder(),
        NoOpBuilder(),
    ])
    def test_has_post_install_hook_default_is_false(
        self, builder: Builder,
    ) -> None:
        # Default is False — cloud-init / Windows / NoOp builders
        # don't need the orchestrator to re-boot for a hook.
        assert builder.has_post_install_hook() is False

    @pytest.mark.parametrize("builder", [
        CloudInitBuilder(),
        WindowsUnattendedBuilder(),
        NoOpBuilder(),
    ])
    def test_post_install_hook_default_is_no_op(self, builder: Builder) -> None:
        # Default hook does nothing.  Builders that need to bake
        # bootstrap state into the cached install artifact (e.g.
        # ProxmoxAnswerBuilder) override.
        comm = MagicMock()
        result = builder.post_install_hook(_linux_vm(), comm)
        assert result is None
        comm.exec.assert_not_called()

    @pytest.mark.parametrize("builder", [
        CloudInitBuilder(),
        WindowsUnattendedBuilder(),
        NoOpBuilder(),
    ])
    def test_post_install_cache_key_extra_default_is_empty(
        self, builder: Builder,
    ) -> None:
        # Default contributes nothing to the cache key.  Overriders
        # return a deterministic content-derived digest so script
        # edits invalidate cached templates.
        assert builder.post_install_cache_key_extra(_linux_vm()) == ""

    @pytest.mark.parametrize("builder", [
        CloudInitBuilder(),
        WindowsUnattendedBuilder(),
        NoOpBuilder(),
    ])
    def test_adopt_prebuilt_default_raises(self, builder: Builder) -> None:
        # Default ``adopt_prebuilt`` raises — mirrors the "you should
        # have checked the phase indicator first" pattern that
        # :meth:`ready_image` uses.  Builders that support the
        # nested-import path (Slice 3 — inner orchestrator imports a
        # bare-metal-built qcow2 instead of running its own ISO-boot
        # install) override this.  Until those overrides land, every
        # concrete builder hits the default raise.
        run = MagicMock()
        cache = MagicMock()
        with pytest.raises(NotImplementedError, match="adopt_prebuilt"):
            builder.adopt_prebuilt(
                _linux_vm(), "/some/prebuilt/disk.qcow2", run, cache,
            )


# ----------------------------------------------------------------------
# CloudInitBuilder
# ----------------------------------------------------------------------


class TestCloudInitBuilderCacheKey:
    def test_returns_24_hex_chars(self) -> None:
        b = CloudInitBuilder()
        key = b.cache_key(_linux_vm())
        assert len(key) == 24
        assert all(c in "0123456789abcdef" for c in key)

    def test_deterministic_across_builder_instances(self) -> None:
        """The builder is stateless — two instances must agree on the
        cache key for the same VM."""
        vm = _linux_vm()
        assert CloudInitBuilder().cache_key(vm) == CloudInitBuilder().cache_key(vm)

    def test_changes_with_iso(self) -> None:
        b = CloudInitBuilder()
        a = _linux_vm(iso="https://example.com/debian-12.qcow2")
        c = _linux_vm(iso="https://example.com/debian-13.qcow2")
        assert b.cache_key(a) != b.cache_key(c)


class TestCloudInitPrepareRunDomain:
    """The run-phase spec must always emit a phase-2 seed ISO for
    cloud-init; no UEFI, no windows flag."""

    def test_seed_iso_always_written(self, tmp_path: Path) -> None:
        from testrange._run import RunDir
        from testrange.cache import CacheManager
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        vm = _linux_vm()

        spec = CloudInitBuilder().prepare_run_domain(
            vm, run,
            mac_ip_pairs=[("aa:bb:cc:dd:ee:01", "", "", "")],
        )

        assert isinstance(spec, RunDomain)
        assert spec.seed_iso is not None
        # Seed ISO now a backend-local ref (string); for local backend
        # it's the absolute path we can Path() and stat.
        assert Path(spec.seed_iso).exists()
        assert spec.uefi is False
        assert spec.windows is False

        # Cleanup — RunDir's __del__ doesn't do this for us.
        run.cleanup()
        del CacheManager  # silence unused


# ----------------------------------------------------------------------
# WindowsUnattendedBuilder
# ----------------------------------------------------------------------


class TestWindowsUnattendedBuilderPrepareInstallDomain:
    def test_install_domain_has_uefi_windows_boot_cdrom(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Windows install mandates UEFI + windows hints + CD-ROM boot
        order.  Those flags are the entire reason this builder exists."""
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        vm = _windows_vm()

        cache = MagicMock()
        cache.stage_local_iso.return_value = tmp_path / "iso-fake.iso"
        cache.get_virtio_win_iso.return_value = tmp_path / "virtio-win.iso"
        # stage_source passes through for a local backend — we return
        # the source path as a string so downstream code sees a ref.
        cache.stage_source.side_effect = lambda p, _b: str(p)

        # Avoid the real pycdlib write by stubbing build_autounattend_iso_bytes.
        import testrange.vms.builders.unattend as un
        monkeypatch.setattr(un, "build_autounattend_iso_bytes", lambda _x: b"iso")

        # Pretend resolve_image returns the iso path as-is.
        monkeypatch.setattr(un, "resolve_image", lambda iso, _cache: Path(iso))

        spec = WindowsUnattendedBuilder().prepare_install_domain(vm, run, cache)

        assert isinstance(spec, InstallDomain)
        assert spec.uefi is True
        assert spec.windows is True
        assert spec.boot_cdrom is True
        # Primary disk is the blank qcow2, not the install ISO.
        assert Path(spec.work_disk).parent == Path(run.path)
        # Extra CD-ROMs: install ISO + virtio-win ISO.
        assert len(spec.extra_cdroms) == 2
        run.cleanup()

    def test_run_domain_has_uefi_windows_no_seed(self, tmp_path: Path) -> None:
        from testrange._run import RunDir
        from testrange.backends.libvirt.storage import LocalStorageBackend

        run = RunDir(LocalStorageBackend(tmp_path))
        spec = WindowsUnattendedBuilder().prepare_run_domain(
            _windows_vm(), run, mac_ip_pairs=[]
        )
        assert isinstance(spec, RunDomain)
        assert spec.seed_iso is None
        assert spec.uefi is True
        assert spec.windows is True
        run.cleanup()


# ----------------------------------------------------------------------
# NoOpBuilder
# ----------------------------------------------------------------------


class TestNoOpBuilderInvariants:
    def test_cache_key_raises(self) -> None:
        """NoOp doesn't use the install cache; the cache key method
        must surface that loudly if anyone calls it."""
        with pytest.raises(NotImplementedError):
            NoOpBuilder().cache_key(_linux_vm())

    def test_prepare_install_domain_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            NoOpBuilder().prepare_install_domain(
                _linux_vm(), MagicMock(), MagicMock()
            )

    def test_install_manifest_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            NoOpBuilder().install_manifest(_linux_vm(), "abc")

    def test_run_domain_linux_is_bios(self, tmp_path: Path) -> None:
        spec = NoOpBuilder(windows=False).prepare_run_domain(
            _noop_vm(tmp_path), MagicMock(), mac_ip_pairs=[]
        )
        assert spec.uefi is False
        assert spec.windows is False
        assert spec.seed_iso is None

    def test_run_domain_windows_is_uefi(self, tmp_path: Path) -> None:
        spec = NoOpBuilder(windows=True).prepare_run_domain(
            _noop_vm(tmp_path, windows=True), MagicMock(), mac_ip_pairs=[]
        )
        assert spec.uefi is True
        assert spec.windows is True
        assert spec.seed_iso is None


class TestAutoSelection:
    """VM.__init__ with ``builder=None`` auto-picks a default based on
    the iso= family.  Windows → WindowsUnattendedBuilder, else CloudInit."""

    def test_linux_cloud_image(self) -> None:
        vm = VM("deb", "https://e/x.qcow2", [Credential("root", "x")])
        assert isinstance(vm.builder, CloudInitBuilder)

    def test_local_qcow2(self, tmp_path: Path) -> None:
        vm = VM("c", str(tmp_path / "x.qcow2"), [Credential("root", "x")])
        assert isinstance(vm.builder, CloudInitBuilder)

    def test_windows_install_iso(self) -> None:
        vm = VM(
            "w", "/srv/Win10_21H1_English_x64.iso",
            [Credential("root", "pw")],
        )
        assert isinstance(vm.builder, WindowsUnattendedBuilder)

    def test_explicit_builder_wins(self) -> None:
        # Windows ISO + explicit NoOpBuilder still gets NoOp.
        vm = VM(
            "w", "/srv/Win10_21H1_English_x64.iso",
            [Credential("root", "pw")],
            builder=NoOpBuilder(windows=True),
        )
        assert isinstance(vm.builder, NoOpBuilder)
        assert vm.builder.windows is True


class TestBuilderRegistry:
    """The auto-selection registry lets third-party code plug in new
    install flavours (BSD preseed, Kickstart, etc.) without touching
    VM.__init__."""

    def _registry_snapshot(self):
        """Capture and restore ``BUILDER_REGISTRY`` so a registration
        inside one test doesn't bleed into the next."""
        from testrange.vms.builders import BUILDER_REGISTRY
        return list(BUILDER_REGISTRY)

    def _restore(self, snapshot) -> None:
        from testrange.vms.builders import BUILDER_REGISTRY
        BUILDER_REGISTRY.clear()
        BUILDER_REGISTRY.extend(snapshot)

    def test_default_picks_cloudinit_for_linux(self) -> None:
        from testrange.vms.builders import (
            CloudInitBuilder,
            auto_select_builder,
        )
        b = auto_select_builder("https://example.com/debian.qcow2")
        assert isinstance(b, CloudInitBuilder)

    def test_default_picks_windows_for_win_iso(self) -> None:
        from testrange.vms.builders import (
            WindowsUnattendedBuilder,
            auto_select_builder,
        )
        b = auto_select_builder("/srv/iso/Win10_21H1_English_x64.iso")
        assert isinstance(b, WindowsUnattendedBuilder)

    @pytest.mark.parametrize("iso", [
        "https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso",
        "/srv/iso/proxmox-ve_8.2-1.iso",
        "proxmox-ve-7.4-1.iso",                      # hyphenated form
        "/cache/PROXMOX-VE_9.0-1.ISO",               # uppercase
    ])
    def test_default_picks_proxmox_for_pve_iso(self, iso: str) -> None:
        from testrange.vms.builders import (
            ProxmoxAnswerBuilder,
            auto_select_builder,
        )
        b = auto_select_builder(iso)
        assert isinstance(b, ProxmoxAnswerBuilder)

    @pytest.mark.parametrize("iso", [
        "https://example.com/debian.qcow2",
        "https://example.com/proxmox-mailgateway-9.iso",  # different product
        "proxmox-backup-server.iso",                       # different product
        "Win10_21H1_English_x64.iso",
    ])
    def test_proxmox_predicate_does_not_match_non_pve(self, iso: str) -> None:
        """Predicate must be tight enough to only catch PVE installer
        ISOs — Proxmox ships several other products (PMG, PBS) that
        share the upstream brand prefix but use different installers."""
        from testrange.vms.builders import (
            ProxmoxAnswerBuilder,
            auto_select_builder,
        )
        b = auto_select_builder(iso)
        assert not isinstance(b, ProxmoxAnswerBuilder)

    def test_custom_predicate_takes_precedence(self) -> None:
        from testrange.vms.builders import (
            NoOpBuilder,
            auto_select_builder,
            register_builder,
        )
        snapshot = self._registry_snapshot()
        try:
            # Register a predicate that always fires — exercises the
            # front-of-list insert.
            register_builder(lambda iso: "debian" in iso, NoOpBuilder)
            b = auto_select_builder("https://example.com/debian.qcow2")
            assert isinstance(b, NoOpBuilder)
        finally:
            self._restore(snapshot)

    def test_prepend_false_registers_as_fallback(self) -> None:
        """With ``prepend=False`` the entry runs only when no earlier
        predicate matches — useful for registering a generic fallback
        that shouldn't shadow the Windows check."""
        from testrange.vms.builders import (
            WindowsUnattendedBuilder,
            auto_select_builder,
            register_builder,
        )
        snapshot = self._registry_snapshot()
        try:
            hits: list[str] = []

            def _trap(iso: str) -> bool:
                hits.append(iso)
                return True

            class _Noop(WindowsUnattendedBuilder):
                pass

            register_builder(_trap, _Noop, prepend=False)
            # Windows predicate in the default position still wins.
            b = auto_select_builder("/srv/Win10.iso")
            assert isinstance(b, WindowsUnattendedBuilder)
            # The fallback predicate was never consulted.
            assert hits == []
        finally:
            self._restore(snapshot)

    def test_vm_uses_registry(self) -> None:
        """Regression: ``VM(iso=...)`` without ``builder=`` consults
        the registry at construction time, so third-party predicates
        are honoured."""
        from testrange import VM, Credential
        from testrange.vms.builders import (
            NoOpBuilder,
            register_builder,
        )
        snapshot = self._registry_snapshot()
        try:
            register_builder(
                lambda iso: "magicstring" in iso,
                NoOpBuilder,
            )
            vm = VM(
                name="x",
                iso="/tmp/something-magicstring.qcow2",
                users=[Credential("root", "pw")],
            )
            assert isinstance(vm.builder, NoOpBuilder)
        finally:
            self._restore(snapshot)
