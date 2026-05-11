"""State-file schema (version 1).

Schema is forward-compatible: resource ``metadata`` dict carries per-kind
fields without a schema bump. ``intent_at`` / ``outcome_at`` separate
"we asked the backend to create this" from "the backend confirmed it,"
so a SIGKILL between record and create still leaves enough information
for cleanup to act safely.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

SCHEMA_VERSION = 1

# Phase constants. Strings, not an Enum — easier to evolve.
PHASE_PREFLIGHT = "preflight"
PHASE_INSTALL = "install"
PHASE_RUN = "run"
PHASE_TEST = "test"
PHASE_CLEANUP = "cleanup"
PHASE_DONE = "done"
PHASE_LEAKED = "leaked"


@dataclass(frozen=True)
class Resource:
    """One backend resource recorded in state.json.

    Fields:
      kind:         resource kind ("network", "pool", "vm", "install_vm",
                    "install_network", "disk_volume", ...).
      backend_name: deterministic name on the backend; cleanup destroys
                    by this name.
      plan_name:    user-facing name from the Plan (None for transient
                    resources that don't appear in the Plan, e.g. the
                    transient install network).
      intent_at:    ISO-8601 timestamp written before the backend call.
      outcome_at:   ISO-8601 timestamp written after the backend confirmed.
                    None means "create-in-flight or failed."
      metadata:     per-kind dict (ip, mac, format, child_run_id, ...).
                    Schema-flexible by design.
    """

    kind: str
    backend_name: str
    plan_name: str | None
    intent_at: str
    outcome_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_outcome(self, outcome_at: str, **extra_metadata: Any) -> Resource:
        new_meta = {**self.metadata, **extra_metadata}
        return replace(self, outcome_at=outcome_at, metadata=new_meta)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "backend_name": self.backend_name,
            "plan_name": self.plan_name,
            "intent_at": self.intent_at,
            "outcome_at": self.outcome_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Resource:
        return cls(
            kind=data["kind"],
            backend_name=data["backend_name"],
            plan_name=data.get("plan_name"),
            intent_at=data["intent_at"],
            outcome_at=data.get("outcome_at"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class State:
    """Run-level state envelope."""

    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    plan_name: str = ""
    driver_class: str = ""
    driver_uri: str = ""
    phase: str = PHASE_PREFLIGHT
    created_at: str = ""
    resources: tuple[Resource, ...] = ()

    def with_resource(self, r: Resource) -> State:
        return replace(self, resources=(*self.resources, r))

    def replace_resource(self, backend_name: str, new: Resource) -> State:
        return replace(
            self,
            resources=tuple(
                new if r.backend_name == backend_name else r for r in self.resources
            ),
        )

    def remove_resource(self, backend_name: str) -> State:
        return replace(
            self,
            resources=tuple(r for r in self.resources if r.backend_name != backend_name),
        )

    def with_phase(self, phase: str) -> State:
        return replace(self, phase=phase)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "plan_name": self.plan_name,
            "driver_class": self.driver_class,
            "driver_uri": self.driver_uri,
            "phase": self.phase,
            "created_at": self.created_at,
            "resources": [r.to_json() for r in self.resources],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> State:
        return cls(
            schema_version=int(data.get("schema_version", 0)),
            run_id=data.get("run_id", ""),
            plan_name=data.get("plan_name", ""),
            driver_class=data.get("driver_class", ""),
            driver_uri=data.get("driver_uri", ""),
            phase=data.get("phase", PHASE_PREFLIGHT),
            created_at=data.get("created_at", ""),
            resources=tuple(Resource.from_json(r) for r in data.get("resources", [])),
        )
