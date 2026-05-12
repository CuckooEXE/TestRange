"""Tests for VM/volume XML rendering in the libvirt driver."""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange.devices import CPU, Memory, OSDrive
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import LibvirtDriver, _render_domain_xml
from testrange.exceptions import DriverError
from testrange.vms import VMSpec


def _spec() -> VMSpec:
    return VMSpec(
        name="web",
        devices=[
            CPU(2),
            Memory(1024),
            OSDrive("p1", 8),
            LibvirtNetworkIface("netA", driver="virtio"),
            LibvirtNetworkIface("netB", driver="e1000"),
        ],
    )


class TestDomainXML:
    def test_required_fields(self, tmp_path: Path) -> None:
        macs = ["52:54:00:aa:bb:cc", "52:54:00:dd:ee:ff"]
        xml = _render_domain_xml(
            "tr_vm_abc_web",
            _spec(),
            os_disk_path=tmp_path / "os.qcow2",
            seed_iso_path=tmp_path / "seed.iso",
            network_refs={"netA": "tr_net_abc_netA", "netB": "tr_net_abc_netB"},
            macs=macs,
        )
        assert "<name>tr_vm_abc_web</name>" in xml
        assert "<memory unit='MiB'>1024</memory>" in xml
        assert "<vcpu>2</vcpu>" in xml
        assert "type='kvm'" in xml
        assert str(tmp_path / "os.qcow2") in xml
        assert str(tmp_path / "seed.iso") in xml
        assert "tr_net_abc_netA" in xml
        assert "tr_net_abc_netB" in xml
        assert "52:54:00:aa:bb:cc" in xml
        assert "52:54:00:dd:ee:ff" in xml
        assert "model type='virtio'" in xml
        assert "model type='e1000'" in xml
        # graphics for virt-viewer
        assert "<graphics type='vnc'" in xml
        assert "listen='127.0.0.1'" in xml
        assert "<video><model type='virtio'/></video>" in xml

    def test_no_seed(self, tmp_path: Path) -> None:
        xml = _render_domain_xml(
            "bn",
            _spec(),
            os_disk_path=tmp_path / "os.qcow2",
            seed_iso_path=None,
            network_refs={"netA": "n1", "netB": "n2"},
            macs=["52:54:00:aa:aa:aa", "52:54:00:bb:bb:bb"],
        )
        assert "cdrom" not in xml

    def test_unknown_network(self, tmp_path: Path) -> None:
        with pytest.raises(DriverError, match="no backend network"):
            _render_domain_xml(
                "bn",
                _spec(),
                os_disk_path=tmp_path / "os.qcow2",
                seed_iso_path=None,
                network_refs={"netA": "n1"},  # netB missing
                macs=["a", "b"],
            )


class TestDestroyDispatch:
    def test_vm_kind(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        called: list[str] = []
        monkeypatch.setattr(d, "destroy_vm", lambda n: called.append(n))
        d.destroy("vm", "tr_vm_abc")
        assert called == ["tr_vm_abc"]

    def test_install_vm_routes_to_destroy_vm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        d = LibvirtDriver(uri="qemu:///session", pool_root=tmp_path)
        called: list[str] = []
        monkeypatch.setattr(d, "destroy_vm", lambda n: called.append(n))
        d.destroy("install_vm", "tr_install_vm_abc")
        assert called == ["tr_install_vm_abc"]
