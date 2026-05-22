"""MockDriver + the new ABC contracts it exercises.

Covers the switch-ownership L2 boundary (create_switch / no bridge methods),
the native-agent capability declaration + preflight gating, and the pool
minimum-capacity preflight check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager
from testrange.communicators import Communicator, NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.base import HypervisorDriver
from testrange.drivers.mock import MockDriver, MockHypervisor
from testrange.networks import Network, Switch
from testrange.orchestrator.install import _install_switch
from testrange.preflight import mgmt_unsupported_findings, native_capability_findings
from testrange.vms import VMRecipe, VMSpec

_Addr = DHCPAddr | StaticAddr | None


def _vm(name: str = "web", *, addr: _Addr = None, comm: Communicator | None = None) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[CPU(1), Memory(512), OSDrive("pool1", 8), NetworkIface("netA", addr=addr)],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
        ),
        communicator=comm or SSHCommunicator("u"),
    )


def _plan(*, addr: _Addr = None, comm: Communicator | None = None) -> Plan:
    return Plan(
        MockHypervisor(
            networks=[Switch("sw1", Network("netA"), cidr="10.0.0.0/24", dhcp=True)],
            pools=[StoragePool("pool1", 32)],
            vms=[_vm(addr=addr, comm=comm)],
        ),
        name="t",
    )


class TestSwitchOwnership:
    def test_driver_satisfies_abc(self) -> None:
        assert isinstance(MockDriver(), HypervisorDriver)

    def test_no_bridge_methods_on_abc_or_driver(self) -> None:
        # L2 is the driver's business via create_switch; the orchestrator
        # never names a bridge, so the bridge API is gone entirely.
        for name in ("create_bridge", "create_isolated_bridge", "destroy_bridge",
                     "compose_bridge_name"):
            assert not hasattr(HypervisorDriver, name)
            assert not hasattr(MockDriver(), name)

    def test_create_switch_returns_uplink_segment_for_nat(self) -> None:
        d = MockDriver()
        nat_switch = Switch(
            "s", Network("a"), cidr="10.0.0.0/24", uplink="eth0", nat=True, dhcp=True
        )
        assert d.create_switch(nat_switch, "tr_switch_s") == "tr_switch_s__uplink"

    def test_create_switch_returns_none_without_nat(self) -> None:
        d = MockDriver()
        plain = Switch("s2", Network("b"), cidr="10.0.1.0/24")
        assert d.create_switch(plain, "tr_switch_s2") is None


class TestNativeCapabilities:
    def test_mock_declares_all_three(self) -> None:
        assert MockDriver().native_guest_capabilities() == frozenset(
            {"execute", "read_file", "write_file"}
        )

    def test_native_communicator_gap_is_error(self) -> None:
        plan = _plan(addr=StaticAddr("10.0.0.100"), comm=NativeCommunicator())
        findings = native_capability_findings(plan, frozenset())
        assert any(f.code == "native-agent-missing-ops" and f.severity == "error" for f in findings)

    def test_native_communicator_satisfied(self) -> None:
        plan = _plan(addr=StaticAddr("10.0.0.100"), comm=NativeCommunicator())
        full = frozenset({"execute", "read_file", "write_file"})
        assert native_capability_findings(plan, full) == ()

    def test_dhcp_discovery_requires_read_file(self) -> None:
        plan = _plan(addr=DHCPAddr())  # SSH comm, DHCP NIC -> needs lease read
        findings = native_capability_findings(plan, frozenset({"execute", "write_file"}))
        assert any(f.code == "dhcp-discovery-unsupported" for f in findings)


class TestMgmtGating:
    """``mgmt=True`` is gated at preflight until ADR-0009 settles its semantics."""

    def _mgmt_plan(self) -> Plan:
        return Plan(
            MockHypervisor(
                networks=[Switch("sw1", Network("netA"), cidr="10.0.0.0/24", mgmt=True)],
                pools=[StoragePool("pool1", 32)],
                vms=[_vm()],
            ),
            name="t",
        )

    def test_mgmt_switch_is_error(self) -> None:
        findings = mgmt_unsupported_findings(self._mgmt_plan())
        assert [f.code for f in findings] == ["mgmt-unsupported"]
        assert findings[0].severity == "error"

    def test_no_mgmt_is_clean(self) -> None:
        assert mgmt_unsupported_findings(_plan()) == ()

    def test_preflight_reports_mgmt_error(self, monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        report = MockDriver().preflight(
            self._mgmt_plan(), cache_manager=CacheManager(),
            install_switch=_install_switch(None),
        )
        assert bool(report) is False
        assert any(f.code == "mgmt-unsupported" for f in report.errors)


class TestPoolCapacityPreflight:
    def _preflight(self, driver: MockDriver, plan: Plan, monkeypatch: pytest.MonkeyPatch,
                   tmp_path: Path) -> object:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        return driver.preflight(
            plan, cache_manager=CacheManager(), install_switch=_install_switch(None)
        )

    def test_pool_exceeding_backing_capacity_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = MockDriver(backing_capacity_gb=16)  # plan asks for a 32 GiB pool
        report = self._preflight(driver, _plan(addr=StaticAddr("10.0.0.100")), monkeypatch, tmp_path)
        assert not report  # error-level finding -> falsy report
        assert any(f.code == "pool-capacity" for f in report.errors)  # type: ignore[attr-defined]

    def test_pool_within_capacity_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = MockDriver(backing_capacity_gb=128)
        report = self._preflight(driver, _plan(addr=StaticAddr("10.0.0.100")), monkeypatch, tmp_path)
        assert report  # no error-level findings
