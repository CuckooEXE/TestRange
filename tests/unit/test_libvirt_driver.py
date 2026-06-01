"""LibvirtDriver keystone (BACKEND-1.0): construction, registry, naming, preflight.

No real libvirt and no libvirt-python needed: the keystone's live surface is
connection + naming + preflight (plan-side, read-only), and the not-yet-built
concern methods raise a clear, phase-tagged DriverError. The lazy SDK import
means the package registers and these tests run with libvirt untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import driver_for_name, is_pinned, scheme_for_hypervisor
from testrange.drivers.base import HypervisorDriver
from testrange.drivers.libvirt import LibvirtDriver, LibvirtHypervisor, LibvirtProfile
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.exceptions import DriverError
from testrange.gateways import SSHJumpGateway
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.build import resolve_build_switch
from testrange.vms import VMRecipe, VMSpec

_BUILD_SW = resolve_build_switch(None)  # an isolated default build switch


def _vm(name: str = "web", *, comm: object | None = None) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                NetworkIface("netA", addr=StaticAddr("10.0.0.150")),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
        ),
        communicator=comm or SSHCommunicator("u"),  # type: ignore[arg-type]
    )


def _plan(comm: object | None = None) -> Plan:
    return Plan(
        "t",
        LibvirtHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 16)],
            vms=[_vm(comm=comm)],
        ),
    )


def _nested_plan() -> Plan:
    """A plan whose one VM requests CPU(nested=True) (for the nested-KVM probe)."""
    return Plan(
        "t",
        LibvirtHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 16)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="host-a",
                        devices=[
                            CPU(2, nested=True),
                            Memory(2048),
                            OSDrive("pool1", 20),
                            NetworkIface("netA", addr=StaticAddr("10.0.0.150")),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"), credentials=[PosixCred("u", password="p")]
                    ),
                    communicator=SSHCommunicator("u"),
                )
            ],
        ),
    )


class TestConstruction:
    def test_satisfies_abc(self) -> None:
        assert isinstance(LibvirtDriver(LibvirtConn()), HypervisorDriver)

    def test_topology_only_scheme_marker(self) -> None:
        # CORE-19: LibvirtHypervisor is a topology-only subclass of the generic
        # Hypervisor; it pins the 'libvirt' scheme and carries no connection.
        hyp = LibvirtHypervisor()
        assert is_pinned(hyp) is True
        assert scheme_for_hypervisor(hyp) == "libvirt"
        assert hyp.networks == () and hyp.pools == () and hyp.vms == ()

    def test_registry_dispatch_by_name_roundtrips_uri(self) -> None:
        drv = LibvirtProfile(uri="qemu:///system").build_driver()
        d = driver_for_name("LibvirtDriver", drv.uri)
        assert isinstance(d, LibvirtDriver)
        assert d.uri == drv.uri


class TestConnRoundTrip:
    def test_to_from_uri(self) -> None:
        conn = LibvirtConn(libvirt_uri="qemu+ssh://root@host/system")
        back = LibvirtConn.from_uri(conn.to_uri())
        assert back == conn

    def test_from_uri_rejects_foreign_scheme(self) -> None:
        with pytest.raises(DriverError, match="teardown URI"):
            LibvirtConn.from_uri("proxmox://x@y")


class TestNaming:
    def test_resource_name_is_libvirt_safe_and_stable(self) -> None:
        d = LibvirtDriver(LibvirtConn())
        n1 = d.compose_resource_name("abcdef1234", "switch", "my switch!")
        n2 = d.compose_resource_name("abcdef1234", "switch", "my switch!")
        assert n1 == n2  # deterministic
        assert all(c.isalnum() or c in "_.-" for c in n1)  # libvirt-safe charset

    def test_mac_is_deterministic_and_locally_administered(self) -> None:
        d = LibvirtDriver(LibvirtConn())
        mac = d.compose_mac("plan", "web", 0)
        assert mac == d.compose_mac("plan", "web", 0)
        assert int(mac.split(":")[0], 16) & 0x02  # locally-administered bit

    def test_build_nic_mac_disjoint_from_declared(self) -> None:
        # BACKEND-7/ADR-0017: the reserved build-NIC sentinel must yield a stable
        # MAC that never collides with any declared NIC index (0..n-1).
        from testrange.drivers.base import BUILD_NIC_NIC_IDX

        d = LibvirtDriver(LibvirtConn())
        build_mac = d.compose_mac("plan", "web", BUILD_NIC_NIC_IDX)
        assert build_mac == d.compose_mac("plan", "web", BUILD_NIC_NIC_IDX)  # deterministic
        declared = {d.compose_mac("plan", "web", i) for i in range(16)}
        assert build_mac not in declared

    def test_volume_ref_and_suffix(self) -> None:
        d = LibvirtDriver(LibvirtConn())
        assert d.compose_volume_ref("poolX", "web.qcow2") == "poolX/web.qcow2"
        assert d.volume_suffix("run_disk") == ".qcow2"
        assert d.volume_suffix("build_seed") == ".iso"


class TestPreflight:
    def test_clean_plan_passes(self) -> None:
        report = LibvirtDriver(LibvirtConn()).preflight(
            _plan(),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert bool(report) is True

    def test_unmapped_uplink_is_rejected(self) -> None:
        # ADR-0016: a Switch.uplink the profile doesn't map fails at preflight,
        # even though L2 realization itself is still BACKEND-1.2.
        plan = Plan(
            "t",
            LibvirtHypervisor(
                networks=[
                    Switch(
                        "sw1",
                        Network("netA"),
                        cidr="10.0.0.0/24",
                        uplink="egress",
                        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
                    )
                ],
                pools=[StoragePool("pool1", 16)],
                vms=[_vm()],
            ),
        )
        report = LibvirtDriver(LibvirtConn()).preflight(
            plan,
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert any(f.code == "unknown-uplink" for f in report.findings)

    def test_nested_cpu_rejected_when_host_nesting_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ADR-0021: CPU(nested=True) on a local host with KVM nesting explicitly
        # off (probe returns False) fails loud.
        monkeypatch.setattr(
            "testrange.drivers.libvirt.driver._probe_host_nested_kvm", lambda: False
        )
        report = LibvirtDriver(LibvirtConn()).preflight(
            _nested_plan(),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert any(f.code == "nested-kvm-disabled" for f in report.findings)

    def test_nested_cpu_passes_when_host_nesting_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("testrange.drivers.libvirt.driver._probe_host_nested_kvm", lambda: True)
        report = LibvirtDriver(LibvirtConn()).preflight(
            _nested_plan(),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert not any(f.code == "nested-kvm-disabled" for f in report.findings)

    def test_nested_cpu_not_rejected_when_host_state_indeterminate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unreadable/empty sysfs param (probe returns None) is indeterminate,
        # not "disabled": it must NOT spuriously fail a legitimate nested plan.
        monkeypatch.setattr("testrange.drivers.libvirt.driver._probe_host_nested_kvm", lambda: None)
        report = LibvirtDriver(LibvirtConn()).preflight(
            _nested_plan(),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert not any(f.code == "nested-kvm-disabled" for f in report.findings)

    def test_nested_cpu_probe_skipped_for_remote_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A remote daemon's sysfs isn't reachable over the libvirt API; skip the
        # probe rather than read the orchestrator host's (irrelevant) sysfs. Even
        # an explicit local "disabled" must not leak into the remote verdict.
        monkeypatch.setattr(
            "testrange.drivers.libvirt.driver._probe_host_nested_kvm", lambda: False
        )
        report = LibvirtDriver(LibvirtConn("qemu+ssh://elsewhere/system")).preflight(
            _nested_plan(),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert not any(f.code == "nested-kvm-disabled" for f in report.findings)


class TestGuestGateway:
    def test_local_libvirt_is_directly_routable(self) -> None:
        # qemu:///system: co-located orchestrator reaches guests directly.
        assert LibvirtDriver(LibvirtConn()).guest_gateway() is None

    def test_remote_qemu_ssh_jumps_through_the_host(self, tmp_path: Path) -> None:
        # ADR-0021: a remote (nested) guest hypervisor's guests are reached by
        # SSH-jumping through the qemu+ssh host with the URI's key.
        key = tmp_path / "k"
        key.write_text("PRIVKEY-TEXT")
        uri = f"qemu+ssh://admin@10.50.0.98/system?keyfile={key}&no_verify=1&sshauth=privkey"
        gw = LibvirtDriver(LibvirtConn(libvirt_uri=uri)).guest_gateway()
        assert isinstance(gw, SSHJumpGateway)
        assert gw.host == "10.50.0.98"
        assert gw.username == "admin"
        assert gw.pkey_text == "PRIVKEY-TEXT"
        assert gw.port == 22
