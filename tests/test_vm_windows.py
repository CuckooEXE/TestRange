"""Unit tests for the Windows install + run path in :mod:`testrange.backends.libvirt.vm`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from testrange.backends.libvirt.vm import VM
from testrange.cache import CacheManager
from testrange.communication.winrm import WinRMCommunicator
from testrange.credentials import Credential
from testrange.devices import HardDrive, Memory, VirtualNetworkRef, vCPU
from testrange.exceptions import VMBuildError


def _win_vm(**overrides) -> VM:
    defaults = dict(
        name="win",
        iso="/srv/iso/Win10_21H1_English_x64.iso",
        users=[
            Credential("root", "Admin1!"),
            Credential("alice", "Alice1!", sudo=True),
        ],
        devices=[
            vCPU(2),
            Memory(4),
            HardDrive(40),
            VirtualNetworkRef("Net", ip="10.50.0.10"),
        ],
    )
    defaults.update(overrides)
    return VM(**defaults)


class TestWindowsDetection:
    """Auto-selection: Windows install ISOs land on the
    :class:`WindowsUnattendedBuilder`; everything else on
    :class:`CloudInitBuilder`.  The builder's
    :meth:`~testrange.vms.builders.base.Builder.default_communicator`
    then drives the default transport."""

    def test_default_communicator_is_winrm(self) -> None:
        from testrange.vms.builders import WindowsUnattendedBuilder
        vm = _win_vm()
        assert isinstance(vm.builder, WindowsUnattendedBuilder)
        assert vm.communicator == "winrm"

    def test_explicit_communicator_wins(self) -> None:
        from testrange.vms.builders import WindowsUnattendedBuilder
        vm = _win_vm(communicator="guest-agent")
        assert isinstance(vm.builder, WindowsUnattendedBuilder)
        assert vm.communicator == "guest-agent"

    def test_linux_vm_not_flagged(self) -> None:
        from testrange.vms.builders import CloudInitBuilder
        vm = VM(
            name="deb",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "x")],
        )
        assert isinstance(vm.builder, CloudInitBuilder)
        assert vm.communicator == "guest-agent"


class TestWindowsDomainXml:
    def _render(self, vm: VM) -> ET.Element:
        xml = vm._base_domain_xml(
            domain_name="tr-win-test",
            disk_path=Path("/tmp/primary.qcow2"),
            seed_iso_path=Path("/tmp/unattend.iso"),
            network_entries=[("tr-Net", "52:54:00:12:34:56")],
            run_id="deadbeef",
            extra_cdroms=[
                Path("/tmp/Win10.iso"),
                Path("/tmp/virtio-win.iso"),
            ],
            boot_cdrom=True,
            uefi=True,
            nvram_path=Path("/tmp/win_VARS.fd"),
            windows=True,
        )
        return ET.fromstring(xml)

    def test_uefi_loader_and_nvram_present(self) -> None:
        root = self._render(_win_vm())
        loader = root.find(".//os/loader")
        assert loader is not None
        assert loader.text == "/usr/share/OVMF/OVMF_CODE_4M.fd"
        assert loader.get("readonly") == "yes"
        nvram = root.find(".//os/nvram")
        assert nvram is not None
        assert nvram.text == "/tmp/win_VARS.fd"
        assert nvram.get("template") == "/usr/share/OVMF/OVMF_VARS_4M.fd"

    def test_boots_cdrom_before_hd(self) -> None:
        root = self._render(_win_vm())
        boots = root.findall(".//os/boot")
        assert [b.get("dev") for b in boots] == ["cdrom", "hd"]

    def test_primary_disk_uses_sata(self) -> None:
        root = self._render(_win_vm())
        disks = root.findall(".//disk[@device='disk']")
        assert len(disks) == 1
        target = disks[0].find("target")
        assert target.get("bus") == "sata"
        assert target.get("dev") == "sda"

    def test_cdroms_shift_past_sata_primary(self) -> None:
        """Primary disk owns sda → CD-ROMs start at sdb."""
        root = self._render(_win_vm())
        cdroms = root.findall(".//disk[@device='cdrom']")
        targets = [cd.find("target").get("dev") for cd in cdroms]
        assert targets == ["sdb", "sdc", "sdd"]

    def test_bootable_cdrom_is_first(self) -> None:
        """With <boot dev='cdrom'/> libvirt assigns bootindex=1 to the
        first CDROM in the device list.  The intended boot medium is the
        Windows install ISO (first of extra_cdroms) — the autounattend
        seed is just attached so Setup finds it by volume scan."""
        root = self._render(_win_vm())
        cdroms = root.findall(".//disk[@device='cdrom']")
        sources = [cd.find("source").get("file") for cd in cdroms]
        assert sources[0] == "/tmp/Win10.iso"
        assert "/tmp/unattend.iso" in sources

    def test_nic_model_is_e1000e(self) -> None:
        root = self._render(_win_vm())
        iface = root.find(".//interface")
        assert iface is not None
        assert iface.find("model").get("type") == "e1000e"


class TestBuildRoutesWindows:
    """The single unified install flow in VM.build() delegates to the
    builder's ``prepare_install_domain`` and ``install_manifest``.
    What makes Windows "Windows" is the builder's output — UEFI, SATA
    disk, extra CD-ROMs — not a special branch inside VM."""

    def test_build_calls_run_install_phase_on_cache_miss(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_cache_root: Path,
    ) -> None:
        vm = _win_vm()
        cache = CacheManager(root=tmp_cache_root)

        called = {}
        def _fake_install(*, conn, cache, run, install_network_name,
                          install_network_mac, h):
            called["args"] = (install_network_name, install_network_mac, h)
            return tmp_cache_root / "fake-cached.qcow2"

        vm._run_install_phase = _fake_install  # type: ignore[method-assign]
        monkeypatch.setattr(cache, "get_vm", lambda _h: None)

        result = vm.build(
            context=MagicMock(),
            cache=cache,
            run=MagicMock(),
            install_network_name="tr-install",
            install_network_mac="aa:bb:cc:dd:ee:99",
        )
        assert result == tmp_cache_root / "fake-cached.qcow2"
        assert called["args"][0] == "tr-install"
        assert called["args"][1] == "aa:bb:cc:dd:ee:99"

    def test_cache_hit_skips_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_cache_root: Path,
    ) -> None:
        vm = _win_vm()
        cache = CacheManager(root=tmp_cache_root)
        cached = tmp_cache_root / "vms" / "cached.qcow2"
        cached.parent.mkdir(exist_ok=True)
        cached.write_bytes(b"cached")
        monkeypatch.setattr(cache, "get_vm", lambda _h: cached)

        # Trip wire: the install path must NOT run.
        def _explode(**_):
            raise AssertionError("install path ran despite cache hit")
        vm._run_install_phase = _explode  # type: ignore[method-assign]

        result = vm.build(
            context=MagicMock(), cache=cache, run=MagicMock(),
            install_network_name="x", install_network_mac="y",
        )
        assert result == cached


class TestWinRMCommunicatorFactory:
    def test_winrm_uses_administrator_for_root(
        self,
        tmp_cache_root: Path,
    ) -> None:
        vm = _win_vm()
        vm._domain = MagicMock()
        pairs = [("52:54:00:12:34:56", "10.50.0.10/24", "10.50.0.1", "10.50.0.1")]
        comm = vm._make_communicator(pairs)
        assert isinstance(comm, WinRMCommunicator)
        # The root credential maps to the built-in Administrator account
        # per the WindowsUnattendedBuilder convention.
        assert comm._username == "Administrator"
        assert comm._password == "Admin1!"

    def test_winrm_without_root_uses_first_credential(
        self,
    ) -> None:
        vm = _win_vm(users=[Credential("alice", "Alice1!", sudo=True)])
        vm._domain = MagicMock()
        pairs = [("52:54:00:12:34:56", "10.50.0.10/24", "", "")]
        comm = vm._make_communicator(pairs)
        assert comm._username == "alice"

    def test_winrm_requires_static_ip(self) -> None:
        vm = _win_vm()
        vm._domain = MagicMock()
        with pytest.raises(VMBuildError, match="static IP"):
            vm._make_communicator(
                [("52:54:00:12:34:56", "", "", "")]
            )
