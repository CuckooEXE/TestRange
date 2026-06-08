"""Tests for the Orchestrator with a fully-mocked driver + cache.

The lifecycle is exercised end-to-end (preflight -> install -> run ->
cleanup) without touching libvirt. Integration coverage on a real
libvirt host lives under tests/integration/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from testrange import Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, NativeCommunicator, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, DHCPAddr, Memory, OSDrive, StaticAddr, StoragePool
from testrange.devices.network import NetworkIface
from testrange.exceptions import (
    BuildNotReadyError,
    BuildRequiredError,
    BuildTimeoutError,
    PreflightError,
)
from testrange.networks import Network, Sidecar, Switch
from testrange.networks.sidecar import LEASEFILE
from testrange.orchestrator import Orchestrator
from testrange.orchestrator.backend import ResolvedBackend
from testrange.packages import Apt
from testrange.preflight import PreflightFinding, PreflightReport
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor


def _plan(name: str = "hello") -> Plan:
    return Plan(
        name,
        MockHypervisor(
            networks=[
                Switch(
                    "sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True)
                ),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=DHCPAddr()),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                        packages=[Apt("nginx")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
    )


def _qga_plan(name: str = "hello") -> Plan:
    """Same shape as ``_plan`` but the VM talks over a NativeCommunicator."""
    return Plan(
        name,
        MockHypervisor(
            networks=[
                Switch(
                    "sw1", Network("netA"), cidr="10.0.1.0/24", sidecar=Sidecar(dhcp=True, dns=True)
                ),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=DHCPAddr()),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                        packages=[Apt("nginx")],
                    ),
                    communicator=NativeCommunicator(),
                ),
            ],
        ),
    )


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MockDriver:
    driver = MockDriver(pool_root=tmp_path / "pools")

    def _fake_resolve(plan: Plan, profile: object) -> ResolvedBackend:
        return ResolvedBackend(
            driver=driver,
            driver_uri="",
        )

    monkeypatch.setattr("testrange.orchestrator.runtime.resolve_backend", _fake_resolve)
    return driver


@pytest.fixture
def populated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[CacheManager, Path]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    cache = LocalCache(root=tmp_path / "c" / "testrange")
    src = tmp_path / "fake-base.qcow2"
    src.write_bytes(b"FAKE-BASE-DISK" * 100)
    cache.add(src, name="debian-13")
    sidecar = tmp_path / "fake-sidecar.qcow2"
    sidecar.write_bytes(b"FAKE-SIDECAR-DISK" * 100)
    cache.add(sidecar, name="testrange-sidecar")
    return CacheManager(local=cache), tmp_path


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real time.sleep in orchestrator tests."""
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh_execute(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[str, ...], float]]:
    """Default SSHCommunicator.execute to a success no-op.

    Lets bring-up paths that call execute (e.g., the builder readiness
    check on a real CloudInitBuilder) complete without real SSH. Tests
    that want to assert what got executed can read the returned list;
    tests that want a failure can override the attribute themselves.
    """
    calls: list[tuple[str, tuple[str, ...], float]] = []

    def fake_execute(
        self: SSHCommunicator,
        argv: Any,
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        calls.append((self.username, tuple(argv), timeout))
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)
    return calls


class TestEnterAndExit:
    def test_full_lifecycle(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr) as orch:
            assert orch.run_id
            assert "web" in orch.vms
        # Both connect + disconnect happened
        names = [c[0] for c in fake_driver.calls]
        assert "connect" in names
        assert "disconnect" in names
        # Install + Run brought up networks/pools/vms
        assert "create_pool" in names
        assert "create_switch" in names  # driver owns L2; no bridge calls
        assert "create_network" in names
        assert "create_vm" in names
        assert "destroy_vm" in names  # run vm torn down on exit
        assert "destroy_switch" in names
        assert "destroy_network" in names
        assert "destroy_pool" in names
        # The libvirt-shaped bridge API is gone — nothing names a bridge.
        assert not any("bridge" in n for n in names)

    def test_build_vm_brought_up_and_torn_down(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        # A build_vm was created (cache miss) and destroyed
        build_creates = [
            c for c in fake_driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]
        ]
        assert len(build_creates) == 1

    def test_cache_hit_skips_build_vm(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        # First run populates the built-disk cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        first_build_creates = sum(
            1 for c in fake_driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]
        )
        assert first_build_creates == 1

        # Reset calls and run again — should hit cache
        fake_driver.calls = []
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        second_build_creates = sum(
            1 for c in fake_driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]
        )
        assert second_build_creates == 0

    def test_preflight_error_aborts(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        fake_driver.preflight_override = PreflightReport(
            findings=(PreflightFinding(code="x", message="nope"),)
        )
        with pytest.raises(PreflightError), Orchestrator(_plan(), cache_manager=mgr):
            pass
        # No state.json was written
        names = [c[0] for c in fake_driver.calls]
        assert "create_pool" not in names

    def test_normal_run_preflights_a_concrete_build_switch(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        # A run that may build (require_cache=False) validates the build switch.
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        preflights = [c for c in fake_driver.calls if c[0] == "preflight"]
        assert preflights and preflights[0][2]["build_switch"] is not None

    def test_require_cache_run_preflights_no_build_switch(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        # A cache-only run (require_cache=True, e.g. a nested inner run) never
        # builds, so the build switch is excluded from preflight (CORE-65).
        # Preflight runs (and is recorded) before the cache-miss gate fires.
        mgr, _ = populated_cache
        with (
            pytest.raises(BuildRequiredError),
            Orchestrator(_plan(), cache_manager=mgr, require_cache=True),
        ):
            pass
        preflights = [c for c in fake_driver.calls if c[0] == "preflight"]
        assert preflights and preflights[0][2]["build_switch"] is None

    def test_build_timeout(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        # A true wedge: the serial sink never emits a result record and never
        # closes (heartbeats forever), so only the watchdog can fire.
        fake_driver.build_result_wedge = True
        # Tiny timeout so the test isn't slow
        with (
            pytest.raises(BuildTimeoutError),
            Orchestrator(_plan(), cache_manager=mgr, build_timeout_s=0.01),
        ):
            pass

    def test_failure_during_bringup_triggers_teardown(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with (
            patch.object(
                fake_driver,
                "create_vm",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError, match="boom"),
            Orchestrator(_plan(), cache_manager=mgr),
        ):
            pass
        names = [c[0] for c in fake_driver.calls]
        # Pool was created and then destroyed during teardown
        assert "create_pool" in names
        assert "destroy_pool" in names


class TestStateFileRecord:
    def test_state_dir_removed_after_clean_exit(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        del fake_driver
        mgr, tmp = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr) as orch:
            run_id = orch.run_id
            state_dir = tmp / "s" / "testrange" / "runs" / run_id
            assert (state_dir / "state.json").exists()
        assert not (tmp / "s" / "testrange" / "runs" / run_id).exists()


class TestHandleLeak:
    """``orch.leak()`` on the handle must flip the parent's flag."""

    def test_leak_method_skips_teardown(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        o = Orchestrator(_plan(), cache_manager=mgr)
        with o as orch:
            orch.leak()
        # The run VM (`tr_vm_<id>_web`) is destroyed only at teardown, which
        # leak() must short-circuit. (The build VM, build pool, and sidecar all
        # fire their destroys mid-build regardless of leak, so they aren't clean
        # sentinels — note `tr_build_vm_` / `tr_build_pool_` are distinct names.)
        run_vm_destroys = [
            c for c in fake_driver.calls if c[0] == "destroy_vm" and c[1][0].startswith("tr_vm_")
        ]
        assert run_vm_destroys == []


def _static_plan(ipv4: str) -> Plan:
    return Plan(
        "hello",
        MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True)),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=StaticAddr(ipv4)),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=SSHCommunicator("u"),
                ),
            ],
        ),
    )


def _two_static_nic_plan(nic_idx: int | None) -> Plan:
    # Two NICs on the SAME network — the case where "by network" is ambiguous
    # and only an index disambiguates the SSH target.
    comm = SSHCommunicator("u", nic_idx=nic_idx) if nic_idx is not None else SSHCommunicator("u")
    return Plan(
        "hello",
        MockHypervisor(
            networks=[
                Switch("sw1", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True)),
            ],
            pools=[StoragePool("pool1", 32)],
            vms=[
                VMRecipe(
                    spec=VMSpec(
                        name="web",
                        devices=[
                            CPU(1),
                            Memory(512),
                            OSDrive("pool1", 8),
                            NetworkIface("netA", addr=StaticAddr("172.31.0.150")),
                            NetworkIface("netA", addr=StaticAddr("172.31.0.151")),
                        ],
                    ),
                    builder=CloudInitBuilder(
                        base=CacheEntry("debian-13"),
                        credentials=[PosixCred("u", password="p")],
                    ),
                    communicator=comm,
                ),
            ],
        ),
    )


class TestNicIdxSelection:
    """SSHCommunicator(nic_idx=) picks which NIC's address to bind to."""

    def test_nic_idx_selects_that_nic(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_two_static_nic_plan(nic_idx=1), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm._host == "172.31.0.151"

    def test_default_binds_first_addressed_nic(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_two_static_nic_plan(nic_idx=None), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm._host == "172.31.0.150"


class TestStaticIPDiscovery:
    """Static-IP NICs short-circuit DHCP lease lookup."""

    def test_static_ip_skips_get_lease_ip(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_static_plan("172.31.0.150"), cache_manager=mgr) as orch:
            web = orch.vms["web"]
            assert isinstance(web.communicator, SSHCommunicator)
            # SSHCommunicator stores the bound host on _host (post-bind).
            assert web.communicator._host == "172.31.0.150"
        # get_lease_ip should not have been called at all — static short-circuits.
        names = [c[0] for c in fake_driver.calls]
        assert "get_lease_ip" not in names

    def test_dhcp_reads_lease_from_sidecar(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        # The default _plan() has a NIC without ipv4 on a dhcp switch, so DHCP
        # discovery must read the lease from the sidecar's dnsmasq lease file
        # (NOT from any hypervisor-managed DHCP).
        mgr, _ = populated_cache
        plan = _plan()
        # Stage a lease for web's first NIC, keyed on its stable MAC.
        mac = fake_driver.compose_mac(plan.name, "web", 0).lower()
        fake_driver.sidecar_leases = f"1700000000 {mac} 10.0.1.55 web *\n"

        with Orchestrator(plan, cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm.host == "10.0.1.55"

        calls = fake_driver.calls
        # The sidecar's lease file was read over the guest-agent transport...
        assert any(c[0] == "native_guest_read_file" and c[1][1] == LEASEFILE for c in calls)
        # ...and the dead hypervisor-side lookup is gone for good.
        assert not any(c[0] == "get_lease_ip" for c in calls)


class TestBuilderReadiness:
    def test_cloudinit_check_runs_after_bind(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
        stub_ssh_execute: list[tuple[str, tuple[str, ...], float]],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        # CloudInitBuilder.wait_ready runs `cloud-init status --wait` once
        # against the bound communicator, with its own inline timeout.
        ready_calls = [c for c in stub_ssh_execute if c[1] == ("cloud-init", "status", "--wait")]
        assert len(ready_calls) == 1
        assert ready_calls[0][2] == 300.0

    def test_failed_check_raises_and_tears_down(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mgr, _ = populated_cache

        def failing_execute(
            self: SSHCommunicator, argv: Any, *, timeout: float = 60.0, cwd: str | None = None
        ) -> ExecResult:
            return ExecResult(exit_code=3, stdout=b"", stderr=b"degraded", duration=0.1)

        monkeypatch.setattr(SSHCommunicator, "execute", failing_execute)
        with (
            pytest.raises(BuildNotReadyError, match="exited 3"),
            Orchestrator(_plan(), cache_manager=mgr),
        ):
            pass
        # Teardown ran even though bring-up failed.
        names = [c[0] for c in fake_driver.calls]
        assert "destroy_vm" in names
        assert "destroy_pool" in names

    def test_builder_noop_skips_check(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
        stub_ssh_execute: list[tuple[str, tuple[str, ...], float]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A builder whose wait_ready is a no-op runs no readiness command.
        monkeypatch.setattr(CloudInitBuilder, "wait_ready", lambda *a, **kw: None)
        mgr, _ = populated_cache
        with Orchestrator(_plan(), cache_manager=mgr):
            pass
        ready_calls = [c for c in stub_ssh_execute if c[1] == ("cloud-init", "status", "--wait")]
        assert ready_calls == []


class TestQGABinding:
    def test_qga_plan_binds_via_driver(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_qga_plan(), cache_manager=mgr) as orch:
            comm = orch.vms["web"].communicator
            assert isinstance(comm, NativeCommunicator)
            assert comm.is_bound is True
        # The QGA path never discovers an IP — get_lease_ip is SSH-only.
        assert not any(c[0] == "get_lease_ip" for c in fake_driver.calls)
        # Builder readiness ran `cloud-init status --wait` through the agent.
        agent_calls = [c for c in fake_driver.calls if c[0] == "native_guest_execute"]
        assert any(c[1][1] == ("cloud-init", "status", "--wait") for c in agent_calls)

    def test_qga_dhcp_nic_waits_for_sidecar_lease(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        # REL-24: a NativeCommunicator VM binds the moment its agent answers,
        # which races the guest's DHCP. The run phase must still gate on every
        # DHCP NIC's lease (read off the sidecar) before handing tests a guest —
        # even though the QGA bind itself needs no IP.
        mgr, _ = populated_cache
        with Orchestrator(_qga_plan(), cache_manager=mgr):
            pass
        assert any(
            c[0] == "native_guest_read_file" and c[1][1] == LEASEFILE for c in fake_driver.calls
        )

    def test_qga_communicator_execute_reaches_driver(
        self,
        fake_driver: MockDriver,
        populated_cache: tuple[CacheManager, Path],
    ) -> None:
        mgr, _ = populated_cache
        with Orchestrator(_qga_plan(), cache_manager=mgr) as orch:
            r = orch.vms["web"].communicator.execute(["id", "-u"])
        assert r.exit_code == 0
        assert any(
            c[0] == "native_guest_execute" and c[1][1] == ("id", "-u") for c in fake_driver.calls
        )
