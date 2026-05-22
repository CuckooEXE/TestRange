"""Tests for state schema (Resource, State)."""

from __future__ import annotations

from testrange.state.schema import (
    PHASE_DONE,
    Resource,
    State,
)


class TestResource:
    def test_roundtrip_json(self) -> None:
        r = Resource(
            kind="network",
            backend_name="tr_network_abc12345_netA",
            plan_name="netA",
            intent_at="2026-05-11T00:00:00Z",
            outcome_at=None,
            metadata={"bridge": "virbr-tr-1"},
        )
        again = Resource.from_json(r.to_json())
        assert again == r

    def test_with_outcome_merges_metadata(self) -> None:
        r = Resource(
            kind="vm",
            backend_name="x",
            plan_name="web",
            intent_at="2026-05-11T00:00:00Z",
            metadata={"a": 1},
        )
        r2 = r.with_outcome("2026-05-11T00:01:00Z", b=2)
        assert r2.outcome_at == "2026-05-11T00:01:00Z"
        assert r2.metadata == {"a": 1, "b": 2}


class TestState:
    def test_with_resource(self) -> None:
        s = State(run_id="r1")
        r = Resource(
            kind="pool",
            backend_name="bn",
            plan_name="p1",
            intent_at="t",
        )
        s2 = s.with_resource(r)
        assert s2.resources == (r,)
        assert s.resources == ()  # immutable

    def test_remove_resource(self) -> None:
        r1 = Resource(kind="pool", backend_name="a", plan_name="p", intent_at="t")
        r2 = Resource(kind="net", backend_name="b", plan_name="n", intent_at="t")
        s = State(resources=(r1, r2))
        s2 = s.remove_resource("a")
        assert s2.resources == (r2,)

    def test_json_roundtrip(self) -> None:
        s = State(
            run_id="r1",
            plan_name="hello",
            driver_class="MockDriver",
            driver_uri="qemu:///session",
            phase=PHASE_DONE,
            created_at="2026-05-11T00:00:00Z",
            resources=(
                Resource(
                    kind="network",
                    backend_name="tr_network_a_b",
                    plan_name="netA",
                    intent_at="2026-05-11T00:00:10Z",
                    outcome_at="2026-05-11T00:00:20Z",
                ),
            ),
        )
        again = State.from_json(s.to_json())
        assert again == s
