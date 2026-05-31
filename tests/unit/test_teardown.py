"""Teardown resilience: bookkeeping failures must not abort the destroys."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from testrange.exceptions import StateError
from testrange.orchestrator.backend import ResolvedBackend
from testrange.orchestrator.context import RunContext
from testrange.orchestrator.teardown import teardown
from testrange.state.schema import PHASE_CLEANUP
from testrange.state.store import StateStore


class _FakeDriver:
    def __init__(self) -> None:
        self.destroyed: list[tuple[str, str]] = []

    def destroy(self, kind: str, backend_name: str, **_metadata: Any) -> None:
        self.destroyed.append((kind, backend_name))


def _ctx(store: StateStore, driver: _FakeDriver) -> RunContext:
    # teardown() only touches store/driver/run_id; plan and cache are unused.
    return RunContext(
        plan=cast(Any, None),
        resolved=ResolvedBackend(driver=cast(Any, driver), driver_uri=""),
        store=store,
        cache=cast(Any, None),
        run_id="r1",
        plan_name="p",
        build_timeout_s=1.0,
        lease_timeout_s=1.0,
        addressing={},
    )


def _store_with_resources(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "run")
    store.initialize(run_id="r1", plan_name="p", driver_class="MockDriver", driver_uri="x")
    store.record_intent(kind="vm", backend_name="tr_vm_a")
    store.record_intent(kind="network", backend_name="tr_net_a")
    return store


def test_set_phase_failure_does_not_abort_destroys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store_with_resources(tmp_path)
    driver = _FakeDriver()

    orig_set_phase = store.set_phase

    def flaky(phase: str) -> None:
        if phase == PHASE_CLEANUP:
            raise StateError("disk full")
        orig_set_phase(phase)

    monkeypatch.setattr(store, "set_phase", flaky)

    teardown(_ctx(store, driver))

    # Both resources were still destroyed despite the phase-set failure...
    assert sorted(driver.destroyed) == [("network", "tr_net_a"), ("vm", "tr_vm_a")]
    # ...and the run dir was cleaned up (state fully drained).
    assert not store.exists()


def test_read_failure_bails_without_destroying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store_with_resources(tmp_path)
    driver = _FakeDriver()

    def boom() -> Any:
        raise StateError("corrupt state")

    monkeypatch.setattr(store, "read", boom)

    teardown(_ctx(store, driver))

    # No resource list to act on -> nothing destroyed (correct: read is the
    # source of truth, not just bookkeeping).
    assert driver.destroyed == []
