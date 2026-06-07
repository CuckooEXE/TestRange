"""ESXI-7: pure naming — resource names, VMware-range MACs, volume refs."""

from __future__ import annotations

import re

import pytest

from testrange.drivers.base import BUILD_NIC_NIC_IDX
from testrange.drivers.esxi import _naming


class TestComposeResourceName:
    def test_deterministic_and_sanitised(self) -> None:
        a = _naming.compose_resource_name("run12345678", "vm", "web")
        assert a == _naming.compose_resource_name("run12345678", "vm", "web")
        assert re.fullmatch(r"[A-Za-z0-9._-]+", a)
        assert a.startswith("tr-vm-run12345-web")

    def test_long_name_truncated_with_hash(self) -> None:
        name = _naming.compose_resource_name("run12345678", "vm", "x" * 120)
        assert len(name) <= 78
        # distinct long inputs stay distinct (hash suffix)
        other = _naming.compose_resource_name("run12345678", "vm", "y" * 120)
        assert name != other


class TestComposeMac:
    def test_in_vmware_manual_range(self) -> None:
        mac = _naming.compose_mac("plan", "vm", 0)
        octs = [int(o, 16) for o in mac.split(":")]
        assert octs[:3] == [0x00, 0x50, 0x56], "must use the VMware OUI"
        assert octs[3] <= 0x3F, "4th octet must be <= 0x3f (manual range ceiling)"

    def test_deterministic(self) -> None:
        assert _naming.compose_mac("p", "v", 1) == _naming.compose_mac("p", "v", 1)

    def test_distinct_per_nic_and_vm(self) -> None:
        macs = {
            _naming.compose_mac("p", "v", 0),
            _naming.compose_mac("p", "v", 1),
            _naming.compose_mac("p", "w", 0),
            _naming.compose_mac("p", "v", BUILD_NIC_NIC_IDX),
        }
        assert len(macs) == 4

    def test_build_nic_sentinel_disjoint_from_declared(self) -> None:
        build = _naming.compose_mac("p", "v", BUILD_NIC_NIC_IDX)
        declared = {_naming.compose_mac("p", "v", i) for i in range(8)}
        assert build not in declared


class TestVolumeRef:
    def test_disk_maps_qcow2_to_vmdk(self) -> None:
        ref = _naming.compose_volume_ref("datastore1", "pool1", "tr-run-web.qcow2")
        assert ref == "[datastore1] pool1/tr-run-web.vmdk"

    def test_iso_passes_through(self) -> None:
        ref = _naming.compose_volume_ref("datastore1", "pool1", "seed.iso")
        assert ref == "[datastore1] pool1/seed.iso"

    def test_parse_round_trip(self) -> None:
        ref = _naming.compose_volume_ref("ds", "p", "d.qcow2")
        ds, path = _naming.parse_volume_ref(ref)
        assert ds == "ds" and path == "p/d.vmdk"

    def test_parse_rejects_non_bracket(self) -> None:
        with pytest.raises(ValueError, match="not an ESXi"):
            _naming.parse_volume_ref("local:import/x.qcow2")

    def test_ref_dir(self) -> None:
        assert _naming.ref_dir("[ds] pool1/web.vmdk") == "pool1"


class TestVolumeSuffix:
    @pytest.mark.parametrize(
        ("kind", "suffix"),
        [
            ("build_disk", ".qcow2"),
            ("run_disk", ".qcow2"),
            ("base_image", ".qcow2"),
            ("build_seed", ".iso"),
            ("boot_iso", ".iso"),
            ("sidecar_config", ".iso"),
        ],
    )
    def test_suffixes(self, kind: str, suffix: str) -> None:
        assert _naming.volume_suffix(kind) == suffix


class TestL2Names:
    def test_stable_and_prefixed(self) -> None:
        assert _naming.vswitch_name("sw").startswith("trs-")
        assert _naming.portgroup_name("n").startswith("trp-")
        assert _naming.mgmt_portgroup_name("sw").startswith("trm-")
        assert _naming.uplink_vswitch_name("vmnic1").startswith("tru-")
        assert _naming.uplink_portgroup_name("sw").startswith("trx-")
        assert _naming.vswitch_name("sw") == _naming.vswitch_name("sw")

    def test_uplink_vswitch_shared_per_pnic(self) -> None:
        # Same pNIC -> same shared uplink vSwitch regardless of the switch.
        assert _naming.uplink_vswitch_name("vmnic1") != _naming.uplink_vswitch_name("vmnic2")
