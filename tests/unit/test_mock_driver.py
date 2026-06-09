"""MockDriver + the new ABC contracts it exercises.

Covers the switch-ownership L2 boundary (create_switch / no bridge methods),
mgmt + unknown-uplink preflight gating, and the pool minimum-capacity
preflight check.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager
from testrange.communicators import Communicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.build import resolve_build_switch
from testrange.preflight import PreflightReport, mgmt_unsupported_findings
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor

_Addr = DHCPAddr | StaticAddr | None


def _vm(
    name: str = "web",
    *,
    addr: _Addr = None,
    comm: Communicator | None = None,
    memory_mb: int = 512,
    cpus: int = 1,
) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(cpus),
                Memory(memory_mb),
                OSDrive("pool1", 8),
                NetworkIface("netA", addr=addr),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("u", password="p")],
        ),
        communicator=comm or SSHCommunicator("u"),
    )


def _plan(
    *,
    addr: _Addr = None,
    comm: Communicator | None = None,
    memory_mb: int = 512,
    cpus: int = 1,
) -> Plan:
    return Plan(
        "t",
        MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="10.0.0.0/24", sidecar=Sidecar(dhcp=True))
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[_vm(addr=addr, comm=comm, memory_mb=memory_mb, cpus=cpus)],
        ),
    )


class TestSwitchOwnership:
    def test_driver_satisfies_abc(self) -> None:
        assert isinstance(MockDriver(), HypervisorDriver)

    def test_no_bridge_methods_on_abc_or_driver(self) -> None:
        # L2 is the driver's business via create_switch; the orchestrator
        # never names a bridge, so the bridge API is gone entirely.
        for name in (
            "create_bridge",
            "create_isolated_bridge",
            "destroy_bridge",
            "compose_bridge_name",
        ):
            assert not hasattr(HypervisorDriver, name)
            assert not hasattr(MockDriver(), name)

    def test_create_switch_returns_uplink_segment_for_nat(self) -> None:
        # uplink is a logical name (ADR-0016) the driver resolves against its
        # profile-supplied map; an unmapped name would fail at preflight.
        d = MockDriver(uplinks={"egress": "br-egress"})
        nat_switch = Switch(
            "s",
            Network("a"),
            cidr="10.0.0.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, nat=True),
        )
        assert d.create_switch(nat_switch, "tr_switch_s") == "tr_switch_s__uplink"

    def test_create_switch_returns_none_without_nat(self) -> None:
        d = MockDriver()
        plain = Switch("s2", Network("b"), cidr="10.0.1.0/24")
        assert d.create_switch(plain, "tr_switch_s2") is None

    def test_create_switch_raises_on_unmapped_uplink(self) -> None:
        d = MockDriver()  # no uplinks mapped
        sw = Switch(
            "s", Network("a"), cidr="10.0.0.0/24", uplink="egress", sidecar=Sidecar(nat=True)
        )
        with pytest.raises(DriverError, match="not mapped"):
            d.create_switch(sw, "tr_switch_s")


class TestDiskPrimitives:
    """ADR-0010 §7: create_blank_volume + resize_volume; data_disk suffix."""

    def test_data_disk_suffix(self) -> None:
        assert MockDriver().volume_suffix("data_disk") == ".qcow2"

    def test_create_blank_volume_writes_sized_file(self, tmp_path: Path) -> None:
        d = MockDriver(pool_root=tmp_path)
        d.create_pool(StoragePool("pool1", 32), "p")
        ref = d.compose_volume_ref("p", "data0.qcow2")
        out = d.create_blank_volume(ref, 16)
        assert out == ref
        assert Path(ref).exists()
        assert b"16G" in Path(ref).read_bytes()
        assert ("create_blank_volume", (str(ref), 16), {}) in d.calls

    def test_resize_volume_grows_and_records(self, tmp_path: Path) -> None:
        d = MockDriver(pool_root=tmp_path)
        d.create_pool(StoragePool("pool1", 32), "p")
        ref = d.compose_volume_ref("p", "os.qcow2")
        d.create_blank_volume(ref, 8)
        out = d.resize_volume(ref, 64)
        assert out == ref
        assert d._volume_sizes[str(ref)] == 64
        assert ("resize_volume", (str(ref), 64), {}) in d.calls

    def test_resize_missing_volume_raises(self, tmp_path: Path) -> None:
        d = MockDriver(pool_root=tmp_path)
        d.create_pool(StoragePool("pool1", 32), "p")
        ref = d.compose_volume_ref("p", "nope.qcow2")
        with pytest.raises(Exception, match="no volume"):
            d.resize_volume(ref, 16)

    def test_resize_shrink_raises(self, tmp_path: Path) -> None:
        d = MockDriver(pool_root=tmp_path)
        d.create_pool(StoragePool("pool1", 32), "p")
        ref = d.compose_volume_ref("p", "os.qcow2")
        d.create_blank_volume(ref, 64)
        with pytest.raises(Exception, match="shrink"):
            d.resize_volume(ref, 8)

    def test_blank_volume_content_survives_download(self, tmp_path: Path) -> None:
        # The sized placeholder round-trips through download_from_pool, which
        # is what lets the build phase capture a data disk into the cache.
        d = MockDriver(pool_root=tmp_path)
        d.create_pool(StoragePool("pool1", 32), "p")
        ref = d.compose_volume_ref("p", "data0.qcow2")
        d.create_blank_volume(ref, 16)
        dest = tmp_path / "captured.qcow2"
        d.download_from_pool(ref, dest)
        assert dest.read_bytes() == Path(ref).read_bytes()


class TestBuildResultSink:
    """CORE-5: the reference build-result sink (live byte generator)."""

    def _drain(self, driver: MockDriver, backend: str = "tr_build_vm_x") -> bytes:
        out = bytearray()
        with closing(driver.read_build_result_sink(backend)) as stream:
            for chunk in stream:
                if not chunk:
                    break  # heartbeat — a wedge would loop forever otherwise
                out.extend(chunk)
        return bytes(out)

    def test_default_stream_is_ok(self) -> None:
        # No knob set: a clean build reports the success token by default, so
        # the existing build-phase happy path keeps working.
        assert self._drain(MockDriver()) == b"TESTRANGE-RESULT: ok\n"

    def test_records_the_call_eagerly(self) -> None:
        # Recorded at call time, not lazily on first iteration — so a teardown
        # driver / call-sequence assertion sees it even if never drained.
        d = MockDriver()
        d.read_build_result_sink("tr_build_vm_x")
        assert ("read_build_result_sink", ("tr_build_vm_x",), {}) in d.calls

    def test_injected_fail_stream(self) -> None:
        d = MockDriver()
        d.build_result_stream = [b'TESTRANGE-RESULT: fail rc=3 cmd="x"\n']
        assert b"fail rc=3" in self._drain(d)

    def test_wedge_emits_only_heartbeats(self) -> None:
        d = MockDriver()
        d.build_result_wedge = True
        # No record, just heartbeats: _drain breaks on the first b"" tick.
        assert self._drain(d) == b""

    def test_stream_is_closeable(self) -> None:
        # The orchestrator wraps the generator in contextlib.closing; closing
        # an unfinished stream must run its finally (here: a clean no-op).
        d = MockDriver()
        stream = d.read_build_result_sink("tr_build_vm_x")
        with closing(stream):
            assert next(stream) == b"TESTRANGE-RESULT: ok\n"


class TestMgmtGating:
    """``mgmt=True`` is gated at preflight until ADR-0009 settles its semantics."""

    def _mgmt_plan(self) -> Plan:
        return Plan(
            "t",
            MockHypervisor(
                networks=[Switch("sw1", Network("netA"), cidr="10.0.0.0/24", mgmt=True)],
                pools=[StoragePool("pool1", 32)],
                vms=[_vm()],
            ),
        )

    def test_mgmt_switch_is_error(self) -> None:
        findings = mgmt_unsupported_findings(self._mgmt_plan())
        assert [f.code for f in findings] == ["mgmt-unsupported"]

    def test_no_mgmt_is_clean(self) -> None:
        assert mgmt_unsupported_findings(_plan()) == ()

    def test_preflight_reports_mgmt_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        report = MockDriver().preflight(
            self._mgmt_plan(),
            cache_manager=CacheManager(),
            build_switch=resolve_build_switch(None),
        )
        assert bool(report) is False
        assert any(f.code == "mgmt-unsupported" for f in report.findings)


class TestUnknownUplinkGating:
    """A Switch.uplink the bound profile doesn't map is preflight-rejected (ADR-0016)."""

    def test_unmapped_uplink_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        plan = Plan(
            "t",
            MockHypervisor(
                networks=[
                    Switch(
                        "sw1",
                        Network("netA"),
                        cidr="10.0.0.0/24",
                        uplink="egress",
                        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
                    )
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_vm()],
            ),
        )
        report = MockDriver().preflight(  # no uplinks mapped
            plan, cache_manager=CacheManager(), build_switch=resolve_build_switch(None)
        )
        assert bool(report) is False
        assert any(f.code == "unknown-uplink" for f in report.findings)

    def test_mapped_uplink_is_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        plan = Plan(
            "t",
            MockHypervisor(
                networks=[
                    Switch(
                        "sw1",
                        Network("netA"),
                        cidr="10.0.0.0/24",
                        uplink="egress",
                        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
                    )
                ],
                pools=[StoragePool("pool1", 32)],
                vms=[_vm()],
            ),
        )
        report = MockDriver(uplinks={"egress": "br0"}).preflight(
            plan, cache_manager=CacheManager(), build_switch=resolve_build_switch(None)
        )
        assert not any(f.code == "unknown-uplink" for f in report.findings)


class TestHostResourcePreflight:
    """The mock surfaces its configured ``backing_*`` capacity through
    ``host_capacity()``; preflight's shared resource gate (CORE-84) turns an
    impossible ask into a blocker."""

    def _preflight(
        self, driver: MockDriver, plan: Plan, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> PreflightReport:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        return driver.preflight(
            plan, cache_manager=CacheManager(), build_switch=resolve_build_switch(None)
        )

    def test_pool_exceeding_backing_capacity_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = MockDriver(backing_capacity_gb=16)  # plan asks for a 32 GiB pool
        report = self._preflight(
            driver, _plan(addr=StaticAddr("10.0.0.100")), monkeypatch, tmp_path
        )
        assert not report  # a finding -> falsy report
        assert any(f.code == "insufficient-storage" for f in report.findings)

    def test_vm_exceeding_host_memory_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = MockDriver(backing_memory_mb=512)  # plan VM asks for more than this
        report = self._preflight(
            driver, _plan(addr=StaticAddr("10.0.0.100"), memory_mb=5_242_880), monkeypatch, tmp_path
        )
        assert not report
        assert any(f.code == "insufficient-memory" for f in report.findings)

    def test_vm_exceeding_host_cpus_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        driver = MockDriver(backing_cpus=2)  # plan VM asks for 8 vCPUs
        report = self._preflight(
            driver, _plan(addr=StaticAddr("10.0.0.100"), cpus=8), monkeypatch, tmp_path
        )
        assert not report
        assert any(f.code == "insufficient-vcpus" for f in report.findings)

    def test_within_capacity_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        driver = MockDriver(backing_capacity_gb=128, backing_memory_mb=65_536, backing_cpus=16)
        report = self._preflight(
            driver, _plan(addr=StaticAddr("10.0.0.100")), monkeypatch, tmp_path
        )
        assert report  # no error-level findings

    def test_unconfigured_capacity_skips_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No backing_* knobs -> host_capacity() is None -> resource gate skipped.
        driver = MockDriver()
        assert driver.host_capacity() is None
        report = self._preflight(
            driver, _plan(addr=StaticAddr("10.0.0.100"), memory_mb=5_242_880), monkeypatch, tmp_path
        )
        assert report  # nothing to compare against -> clean
