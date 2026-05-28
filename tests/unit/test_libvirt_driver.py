"""LibvirtDriver keystone (BACKEND-1.1): construction, registry, naming, preflight.

No real libvirt and no libvirt-python/pyroute2 needed: the slice's live surface
is connection + naming + preflight (plan-side, read-only), and the not-yet-built
methods raise a clear DriverError. The lazy SDK imports mean the package
registers and these tests run with neither SDK touched.
"""

from __future__ import annotations

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers import driver_for, driver_for_name
from testrange.drivers.base import HypervisorDriver
from testrange.drivers.libvirt import LibvirtDriver, LibvirtHypervisor
from testrange.drivers.libvirt._conn import LibvirtConn
from testrange.exceptions import DriverError
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.orchestrator.build import resolve_build_switch
from testrange.vms import VMRecipe, VMSpec

_BUILD_SW = resolve_build_switch(None)[0]  # an isolated default build switch (unused by preflight)


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


def _plan(*, build_switch: object = None, comm: object | None = None) -> Plan:
    return Plan(
        "t",
        LibvirtHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 16)],
            vms=[_vm(comm=comm)],
            build_switch=build_switch,  # type: ignore[arg-type]
        ),
    )


class TestConstruction:
    def test_satisfies_abc(self) -> None:
        assert isinstance(LibvirtDriver(LibvirtConn()), HypervisorDriver)

    def test_registry_dispatch_by_hypervisor_type(self) -> None:
        assert isinstance(driver_for(LibvirtHypervisor()), LibvirtDriver)

    def test_registry_dispatch_by_name_roundtrips_uri(self) -> None:
        hyp = LibvirtHypervisor(uri="qemu:///system", backing_pool="images")
        d = driver_for_name("LibvirtDriver", hyp.driver_uri)
        assert isinstance(d, LibvirtDriver)
        assert d.uri == hyp.driver_uri

    def test_hypervisor_defaults(self) -> None:
        hyp = LibvirtHypervisor()
        assert hyp.uri == "qemu:///system"
        assert hyp.backing_pool == "default"
        assert hyp.build_switch is None
        assert hyp.all_switches == ()

    def test_empty_uri_rejected(self) -> None:
        with pytest.raises(ValueError, match="uri"):
            LibvirtHypervisor(uri="")


class TestConnRoundTrip:
    def test_to_from_uri(self) -> None:
        conn = LibvirtConn(libvirt_uri="qemu+ssh://root@host/system", backing_pool="images")
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

    def test_managed_build_switch_rejected_until_realized(self) -> None:
        # supports_managed_build_egress is False until BACKEND-1.2.
        assert LibvirtDriver(LibvirtConn()).supports_managed_build_egress is False
        report = LibvirtDriver(LibvirtConn()).preflight(
            _plan(build_switch=ManagedBuildSwitch(uplink="virbr0")),
            cache_manager=None,  # type: ignore[arg-type]
            build_switch=_BUILD_SW,
        )
        assert bool(report) is False
        assert any(f.code == "managed-build-egress-unsupported" for f in report.findings)


class TestUnimplementedSurfaceFailsLoud:
    def test_l2_storage_vm_snapshot_raise_clear_errors(self) -> None:
        d = LibvirtDriver(LibvirtConn())
        for call in (
            lambda: d.destroy_switch("x"),
            lambda: d.create_pool(StoragePool("p", 8), "x"),
            lambda: d.start_vm("x"),
            lambda: d.list_snapshots("x"),
        ):
            with pytest.raises(DriverError, match="BACKEND-1"):
                call()
