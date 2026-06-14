"""Tests for the DAG executor walks: ordering, ledger stamps, --resume (DAG-6/8/9).

Driven end-to-end through the Orchestrator against MockDriver, like
``test_orchestrator.py`` — the executor is the part of the lifecycle these
pin: wave-ordered realize (``.needs()`` is honored), per-node completion
stamps, and the resume path that reattaches instead of re-creating.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from testrange import Orchestrator, Plan
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry, CacheManager, LocalCache
from testrange.communicators import ExecResult, SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.exceptions import StateError
from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.executor import probe_misses
from testrange.vms import VMRecipe, VMSpec
from tests.mock_driver import MockDriver, MockHypervisor


def _two_tier_plan(name: str = "two-tier") -> Plan:
    hyp = MockHypervisor()
    hyp.add_pool(StoragePool("pool1", 32))
    hyp.add_switch(Switch("sw1", Network("netA"), cidr="172.31.0.0/24", sidecar=Sidecar(dhcp=True)))

    def _vm(vm_name: str, ip: str) -> VMRecipe:
        return VMRecipe(
            spec=VMSpec(
                name=vm_name,
                devices=[
                    CPU(1),
                    Memory(512),
                    OSDrive(hyp.pools["pool1"], 8),
                    NetworkIface(hyp.networks["netA"], addr=StaticAddr(ip)),
                ],
            ),
            builder=CloudInitBuilder(
                base=CacheEntry("debian-13"),
                credentials=[PosixCred("u", password="p")],
            ),
            communicator=SSHCommunicator("u"),
        )

    db = hyp.add_vm(_vm("db", "172.31.0.110"))
    web = hyp.add_vm(_vm("web", "172.31.0.120"))
    web.needs(db)
    return Plan(name, hyp)


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MockDriver:
    driver = MockDriver(pool_root=tmp_path / "pools")

    def _fake_resolve(plan: Plan, profile: object) -> ResolvedBackend:
        return ResolvedBackend(driver=driver, driver_uri="")

    monkeypatch.setattr("testrange.orchestrator.runtime.resolve_backend", _fake_resolve)
    return driver


@pytest.fixture
def populated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[CacheManager, Path]:
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
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def stub_ssh_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute(
        self: SSHCommunicator,
        argv: Any,
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration=0.0)

    monkeypatch.setattr(SSHCommunicator, "execute", fake_execute)


def _run_vm_creates(driver: MockDriver) -> list[str]:
    """Run-VM create calls (``tr_vm_…``) in chronological order."""
    return [c[1][0] for c in driver.calls if c[0] == "create_vm" and c[1][0].startswith("tr_vm_")]


class TestRealizeOrdering:
    def test_needs_orders_run_vm_creation(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        """``web.needs(db)`` puts db's realize wave strictly before web's."""
        mgr, _ = populated_cache
        with Orchestrator(_two_tier_plan(), cache_manager=mgr):
            pass
        creates = _run_vm_creates(fake_driver)
        assert len(creates) == 2
        assert "db" in creates[0]
        assert "web" in creates[1]

    def test_builds_stay_concurrent_one_build_pool(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        """Ordering edges gate the run, not the build: one shared build infra
        services both VM builds in the single content wave."""
        mgr, _ = populated_cache
        with Orchestrator(_two_tier_plan(), cache_manager=mgr):
            pass
        build_pools = [c for c in fake_driver.calls if c[0] == "create_pool" and "build" in c[1][1]]
        assert len(build_pools) == 1
        build_creates = [
            c for c in fake_driver.calls if c[0] == "create_vm" and "build_vm" in c[1][0]
        ]
        assert len(build_creates) == 2


class TestCompletionLedger:
    def test_walks_stamp_every_node(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        mgr, _ = populated_cache
        o = Orchestrator(_two_tier_plan(), cache_manager=mgr)
        with o:
            expected = {"pool:pool1", "network:sw1", "sidecar:sw1", "vm:db", "vm:web"}
            assert o.ctx.materialized_nodes == expected
            assert o.ctx.realized_nodes == expected
            state = o.ctx.store.read()
            record = state.node_record("vm:web")
            assert record is not None
            assert record.materialized_at is not None
            assert record.realized_at is not None

    def test_probe_misses_names_unbuilt_vms(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        mgr, _ = populated_cache
        plan = _two_tier_plan()
        o = Orchestrator(plan, cache_manager=mgr)
        assert probe_misses(o.ctx, plan.graph) == ["db", "web"]


class TestResume:
    def test_resume_reattaches_instead_of_recreating(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        """A leaked (dead-owner) run resumes: no second create for realized
        nodes, fresh communicators bind, and teardown still drains the
        original ledger (DAG-9)."""
        mgr, _ = populated_cache
        first = Orchestrator(_two_tier_plan(), cache_manager=mgr)
        with first as orch:
            orch.leak()
        run_id = first.run_id
        assert len(_run_vm_creates(fake_driver)) == 2

        fake_driver.calls = []
        # A fresh Plan object: a resume happens from a new process, with new
        # (unbound) communicator instances.
        second = Orchestrator(_two_tier_plan(), cache_manager=mgr, run_id=run_id, resume=True)
        with second as orch:
            assert sorted(orch.vms) == ["db", "web"]
            comm = orch.vms["web"].communicator
            assert isinstance(comm, SSHCommunicator)
            assert comm.is_bound
        # Reattach: nothing was re-created...
        names = [c[0] for c in fake_driver.calls]
        assert _run_vm_creates(fake_driver) == []
        assert "create_pool" not in names
        assert "create_switch" not in names
        # ...and the run's resources were torn down at exit via the reopened
        # state ledger.
        destroyed = [c[1][0] for c in fake_driver.calls if c[0] == "destroy_vm"]
        assert any(name.startswith("tr_vm_") for name in destroyed)

    def test_resume_requires_run_id(self) -> None:
        with pytest.raises(ValueError, match="requires the run_id"):
            Orchestrator(_two_tier_plan(), resume=True)

    def test_resume_rejects_foreign_plan(
        self, fake_driver: MockDriver, populated_cache: tuple[CacheManager, Path]
    ) -> None:
        mgr, _ = populated_cache
        first = Orchestrator(_two_tier_plan(), cache_manager=mgr)
        with first as orch:
            orch.leak()
        with (
            pytest.raises(StateError, match="refusing to resume"),
            Orchestrator(
                _two_tier_plan("other-plan"),
                cache_manager=mgr,
                run_id=first.run_id,
                resume=True,
            ),
        ):
            pass
