"""Unit tests for the rich-free dashboard state model (ADR-0029).

Covers the thread-safety contract (many worker threads writing while the render
thread snapshots), ring-buffer eviction, stage overwrite, and the abort sweep.
No rich, no TTY.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from testrange.orchestrator.dashboard_state import DashboardState, VMStage


def test_seed_then_advance_overwrites_stage_and_stamps_start() -> None:
    s = DashboardState()
    s.seed_vms(["web", "db"])
    snap = s.snapshot()
    assert [v.name for v in snap.vms] == ["web", "db"]  # plan order preserved
    assert all(v.stage is VMStage.PENDING and v.elapsed is None for v in snap.vms)

    s.set_vm_stage("web", VMStage.BOOTING)
    web = {v.name: v for v in s.snapshot().vms}["web"]
    assert web.stage is VMStage.BOOTING
    assert web.elapsed is not None and web.elapsed >= 0.0  # first activity stamped


def test_set_stage_registers_unseen_vm() -> None:
    s = DashboardState()
    s.set_vm_stage("late", VMStage.BUILDING)
    names = {v.name for v in s.snapshot().vms}
    assert "late" in names


def test_started_at_is_stable_across_later_transitions() -> None:
    s = DashboardState()
    s.set_vm_stage("web", VMStage.PROVISIONING)
    first = {v.name: v for v in s.snapshot().vms}["web"].elapsed
    s.set_vm_stage("web", VMStage.READY)
    second = {v.name: v for v in s.snapshot().vms}["web"].elapsed
    assert first is not None and second is not None
    assert second >= first  # elapsed only grows; start was not reset


def test_pending_does_not_stamp_start() -> None:
    s = DashboardState()
    s.set_vm_stage("web", VMStage.PENDING)
    assert s.snapshot().vms[0].elapsed is None


def test_detail_is_retained_when_not_overwritten() -> None:
    s = DashboardState()
    s.set_vm_stage("web", VMStage.FAILED, detail="boom")
    s.set_vm_stage("web", VMStage.FAILED)  # no detail → keep the prior one
    assert s.snapshot().vms[0].detail == "boom"


def test_abort_sweeps_only_non_terminal_vms() -> None:
    s = DashboardState()
    s.seed_vms(["a", "b", "c"])
    s.set_vm_stage("a", VMStage.READY)
    s.set_vm_stage("b", VMStage.FAILED, detail="real cause")
    s.set_vm_stage("c", VMStage.BOOTING)
    s.abort_unfinished()
    by = {v.name: v for v in s.snapshot().vms}
    assert by["a"].stage is VMStage.READY  # untouched
    assert by["b"].stage is VMStage.FAILED and by["b"].detail == "real cause"  # culprit kept
    assert by["c"].stage is VMStage.FAILED  # swept
    assert by["c"].detail is not None


def test_tests_track_running_then_outcome() -> None:
    s = DashboardState()
    s.start_test("t1")
    assert s.snapshot().tests[0].status == "running"
    s.finish_test("t1", passed=False, duration=1.5, error="AssertionError")
    t = s.snapshot().tests[0]
    assert t.status == "failed" and t.duration == 1.5 and t.error == "AssertionError"


def test_log_ring_evicts_oldest() -> None:
    s = DashboardState(log_maxlen=3)
    for i in range(5):
        s.append_log(f"line {i}")
    assert s.snapshot().log_lines == ("line 2", "line 3", "line 4")


def test_serial_ring_tags_vm_and_evicts() -> None:
    s = DashboardState(serial_maxlen=2)
    s.append_serial("web", "boot 1")
    s.append_serial("web", "boot 2")
    s.append_serial("db", "boot 3")
    assert s.snapshot().serial_lines == (("web", "boot 2"), ("db", "boot 3"))


def test_snapshot_is_isolated_from_later_mutation() -> None:
    s = DashboardState()
    s.set_vm_stage("web", VMStage.BOOTING)
    snap = s.snapshot()
    s.set_vm_stage("web", VMStage.READY)  # mutate after snapshotting
    assert snap.vms[0].stage is VMStage.BOOTING  # frozen view unaffected


def test_concurrent_writers_and_snapshots_stay_consistent() -> None:
    """Hammer the state from many threads; snapshots must never tear or crash."""
    s = DashboardState()
    names = [f"vm{i}" for i in range(16)]
    s.seed_vms(names)

    def work(i: int) -> None:
        name = names[i % len(names)]
        for stage in (VMStage.PROVISIONING, VMStage.BOOTING, VMStage.BINDING, VMStage.READY):
            s.set_vm_stage(name, stage)
            s.append_log(f"{name} -> {stage.value}")
            s.append_serial(name, f"{name} chatter")
            s.snapshot()  # concurrent reads

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(64)))

    snap = s.snapshot()
    assert len(snap.vms) == len(names)
    assert all(v.stage is VMStage.READY for v in snap.vms)
