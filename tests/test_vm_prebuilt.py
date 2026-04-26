"""Unit tests for the NoOpBuilder (BYOI / prebuilt) flow through the VM API.

These exercise the public surface: construct a VM with
``builder=NoOpBuilder()`` and assert the VM + builder interact correctly
(communicator defaults, image staging, build dispatch, domain XML).
Low-level NoOpBuilder unit tests live in ``tests/test_builders.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from testrange import Credential, NoOpBuilder
from testrange import LibvirtVM as VM
from testrange.backends.libvirt.guest_agent import GuestAgentCommunicator
from testrange.cache import CacheManager
from testrange.communication.ssh import SSHCommunicator
from testrange.devices import HardDrive, vNIC
from testrange.exceptions import VMBuildError


def _writable_qcow2(path: Path) -> Path:
    path.write_bytes(b"QFI\xfbfake-qcow2-stub")
    return path


def _local_run(cache_root: Path):
    """Return a RunDir backed by a LocalStorageBackend at *cache_root*."""
    from testrange._run import RunDir
    from testrange.backends.libvirt.storage import LocalStorageBackend
    return RunDir(LocalStorageBackend(cache_root))


def _noop_vm(
    tmp_path: Path,
    *,
    users: list[Credential] | None = None,
    windows: bool = False,
    communicator: str | None = None,
    devices: list | None = None,
    name: str = "byoi",
) -> VM:
    """Build a NoOpBuilder-backed VM with a writable fake qcow2."""
    src = _writable_qcow2(tmp_path / f"{name}.qcow2")
    return VM(
        name=name,
        iso=str(src),
        users=users or [Credential("deploy", "pw")],
        devices=devices or [vNIC("Net", ip="10.0.0.5")],
        builder=NoOpBuilder(windows=windows),
        communicator=communicator,
    )


class TestNoOpDefaults:
    def test_default_communicator_linux(self, tmp_path: Path) -> None:
        vm = _noop_vm(tmp_path)
        assert vm.communicator == "guest-agent"

    def test_default_communicator_windows(self, tmp_path: Path) -> None:
        vm = _noop_vm(tmp_path, windows=True)
        assert vm.communicator == "winrm"

    def test_explicit_communicator_wins(self, tmp_path: Path) -> None:
        vm = _noop_vm(tmp_path, communicator="ssh")
        assert vm.communicator == "ssh"

    def test_rejects_unknown_communicator(self, tmp_path: Path) -> None:
        with pytest.raises(VMBuildError, match="communicator="):
            _noop_vm(tmp_path, communicator="bogus")


class TestReadyImage:
    """Tests for NoOpBuilder.ready_image() via VM.build()."""

    def test_copies_when_outside_cache_root(
        self,
        tmp_path: Path,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        src = tmp_path / "outside" / "golden.qcow2"
        src.parent.mkdir()
        src.write_bytes(b"PREBUILT_CONTENTS")
        vm = VM(
            name="byoi",
            iso=str(src),
            users=[Credential("deploy", "pw")],
            devices=[vNIC("Net", ip="10.0.0.5")],
            builder=NoOpBuilder(),
        )
        cache = CacheManager(root=tmp_cache_root)

        monkeypatch.setattr(
            "testrange.storage.disk.qcow2._qemu_img_info",
            lambda _: {"format": "qcow2"},
        )
        run = _local_run(tmp_cache_root)

        dest = Path(vm.builder.ready_image(vm, cache, run))

        assert dest.exists()
        # New layout: BYOI sits in its own per-VM directory keyed by
        # ``byoi-<sha>``, with the standard disk + manifest pair inside.
        assert dest.parent.parent == cache.vms_dir
        assert dest.parent.name.startswith("byoi-")
        assert dest.read_bytes() == b"PREBUILT_CONTENTS"
        manifest = dest.parent / "manifest.json"
        assert manifest.exists()
        meta = json.loads(manifest.read_text())
        assert meta["prebuilt"] is True
        assert meta["source_path"] == str(src.resolve())

    def test_idempotent_second_call(
        self,
        tmp_path: Path,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        src = _writable_qcow2(tmp_path / "golden.qcow2")
        vm = VM(
            name="byoi",
            iso=str(src),
            users=[Credential("deploy", "pw")],
            devices=[vNIC("Net", ip="10.0.0.5")],
            builder=NoOpBuilder(),
        )
        cache = CacheManager(root=tmp_cache_root)
        monkeypatch.setattr(
            "testrange.storage.disk.qcow2._qemu_img_info",
            lambda _: {"format": "qcow2"},
        )
        run = _local_run(tmp_cache_root)

        first = Path(vm.builder.ready_image(vm, cache, run))
        first_mtime = first.stat().st_mtime_ns
        second = Path(vm.builder.ready_image(vm, cache, run))

        assert first == second
        assert second.stat().st_mtime_ns == first_mtime  # no re-copy

    def test_uses_path_in_place_when_inside_cache_root(
        self,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = CacheManager(root=tmp_cache_root)
        # Place a source file under the cache root.
        src = cache.vms_dir / "user-staged.qcow2"
        src.write_bytes(b"PREBUILT_CONTENTS")
        vm = VM(
            name="byoi",
            iso=str(src),
            users=[Credential("deploy", "pw")],
            devices=[vNIC("Net", ip="10.0.0.5")],
            builder=NoOpBuilder(),
        )
        monkeypatch.setattr(
            "testrange.storage.disk.qcow2._qemu_img_info",
            lambda _: {"format": "qcow2"},
        )
        run = _local_run(tmp_cache_root)

        dest = Path(vm.builder.ready_image(vm, cache, run))
        assert dest == src  # no copy happened

    def test_missing_file_raises(self, tmp_cache_root: Path) -> None:
        vm = VM(
            name="byoi",
            iso="/nonexistent/path/ghost.qcow2",
            users=[Credential("root", "pw")],
            builder=NoOpBuilder(),
        )
        cache = CacheManager(root=tmp_cache_root)
        run = _local_run(tmp_cache_root)
        with pytest.raises(VMBuildError, match="not found"):
            vm.builder.ready_image(vm, cache, run)

    def test_wrong_format_raises(
        self,
        tmp_path: Path,
        tmp_cache_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        src = _writable_qcow2(tmp_path / "golden.qcow2")
        vm = VM(
            name="byoi",
            iso=str(src),
            users=[Credential("deploy", "pw")],
            devices=[vNIC("Net", ip="10.0.0.5")],
            builder=NoOpBuilder(),
        )
        cache = CacheManager(root=tmp_cache_root)
        monkeypatch.setattr(
            "testrange.storage.disk.qcow2._qemu_img_info",
            lambda _: {"format": "raw"},
        )
        run = _local_run(tmp_cache_root)

        with pytest.raises(VMBuildError, match="not qcow2"):
            vm.builder.ready_image(vm, cache, run)


class TestResolveCommunicatorHost:
    def _make_vm(self, **overrides: Any) -> VM:
        defaults: dict[str, Any] = dict(
            name="x",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
        )
        defaults.update(overrides)
        return VM(**defaults)

    def test_returns_first_static_ip(self) -> None:
        vm = self._make_vm()
        pairs = [
            ("aa:bb:cc:dd:ee:01", "", "", ""),
            ("aa:bb:cc:dd:ee:02", "10.0.0.5/24", "10.0.0.1", "10.0.0.1"),
            ("aa:bb:cc:dd:ee:03", "10.0.1.5/24", "", ""),
        ]
        assert vm._resolve_communicator_host(pairs) == "10.0.0.5"

    def test_raises_when_no_static_ip(self) -> None:
        vm = self._make_vm()
        pairs = [
            ("aa:bb:cc:dd:ee:01", "", "", ""),
            ("aa:bb:cc:dd:ee:02", "", "", ""),
        ]
        with pytest.raises(VMBuildError, match="static IP"):
            vm._resolve_communicator_host(pairs)


class TestMakeCommunicator:
    def test_guest_agent(self, tmp_path: Path) -> None:
        vm = _noop_vm(tmp_path, communicator="guest-agent")
        vm._domain = MagicMock()
        comm = vm._make_communicator([])
        assert isinstance(comm, GuestAgentCommunicator)

    def test_ssh_uses_first_credential_and_static_ip(self, tmp_path: Path) -> None:
        vm = _noop_vm(
            tmp_path,
            users=[Credential("deploy", "secret")],
            communicator="ssh",
        )
        pairs = [("aa:bb:cc:dd:ee:02", "10.0.0.7/24", "10.0.0.1", "10.0.0.1")]
        comm = vm._make_communicator(pairs)
        assert isinstance(comm, SSHCommunicator)
        assert comm._host == "10.0.0.7"
        assert comm._username == "deploy"
        assert comm._password == "secret"

    def test_ssh_prefers_credential_with_ssh_key(self, tmp_path: Path) -> None:
        vm = _noop_vm(
            tmp_path,
            users=[
                Credential("root", "rootpw"),
                Credential("deploy", "deploypw", ssh_key="ssh-ed25519 AAA"),
            ],
            communicator="ssh",
        )
        pairs = [("aa:bb:cc:dd:ee:02", "10.0.0.7/24", "10.0.0.1", "10.0.0.1")]
        comm = vm._make_communicator(pairs)
        assert isinstance(comm, SSHCommunicator)
        assert comm._username == "deploy"


class TestBaseDomainXmlSeedOptional:
    def test_no_cdrom_when_seed_iso_is_none(self, tmp_path: Path) -> None:
        import xml.etree.ElementTree as ET

        vm = _noop_vm(
            tmp_path,
            devices=[HardDrive(10), vNIC("Net", ip="10.0.0.5")],
            communicator="ssh",
        )
        xml = vm._base_domain_xml(
            domain_name="tr-byoi-xxxx",
            disk_path=Path("/tmp/overlay.qcow2"),
            seed_iso_path=None,
            network_entries=[("tr-test", "aa:bb:cc:dd:ee:01")],
            run_id="deadbeef",
        )
        root = ET.fromstring(xml)
        cdroms = root.findall(".//disk[@device='cdrom']")
        assert cdroms == []

    def test_cdrom_present_when_seed_iso_passed(self) -> None:
        import xml.etree.ElementTree as ET

        vm = VM(
            name="cloud",
            iso="https://example.com/debian.qcow2",
            users=[Credential("root", "pw")],
            devices=[HardDrive(10), vNIC("Net")],
        )
        xml = vm._base_domain_xml(
            domain_name="tr-cloud-xxxx",
            disk_path=Path("/tmp/overlay.qcow2"),
            seed_iso_path=Path("/tmp/seed.iso"),
            network_entries=[("tr-test", "aa:bb:cc:dd:ee:01")],
            run_id="deadbeef",
        )
        root = ET.fromstring(xml)
        cdroms = root.findall(".//disk[@device='cdrom']")
        assert len(cdroms) == 1
        source = cdroms[0].find("source")
        assert source is not None
        assert source.get("file") == "/tmp/seed.iso"


class TestBuildDispatchesNoOp:
    def test_build_calls_ready_image(self, tmp_path: Path) -> None:
        """build() for a NoOp VM must invoke builder.ready_image and
        bypass the install-phase entirely."""
        vm = _noop_vm(tmp_path)
        cache = MagicMock()
        run = MagicMock()
        sentinel_ref = str(tmp_path / "sentinel.qcow2")
        vm.builder.ready_image = MagicMock(return_value=sentinel_ref)  # type: ignore[method-assign]

        result = vm.build(
            context=MagicMock(),
            cache=cache,
            run=run,
            install_network_name="",
            install_network_mac="",
        )
        assert result == sentinel_ref
        vm.builder.ready_image.assert_called_once_with(vm, cache, run)
