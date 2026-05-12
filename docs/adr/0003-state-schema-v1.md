# ADR-0003: State schema v1 — designed for resume + nested

Status: Accepted
Date: 2026-05-11

## Context

PLAN.md decision 4 commits the state-file schema to be future-proof
for resume and nested orchestration even though neither feature
ships in v0. .bak retrofitted the schema repeatedly and the cost was
material.

## Decision

State file format (``schema_version: 1``):

```json
{
  "schema_version": 1,
  "run_id": "...",
  "plan_name": "...",
  "driver_class": "LibvirtDriver",
  "driver_uri": "qemu:///system",
  "phase": "install",
  "created_at": "2026-05-11T...",
  "resources": [
    {
      "kind": "install_disk",
      "backend_name": "tr_install_vm_abc12345_web.qcow2",
      "plan_name": "web",
      "intent_at": "2026-05-11T...",
      "outcome_at": "2026-05-11T...",
      "metadata": {"pool_backend": "tr_pool_abc12345_pool1"}
    }
  ]
}
```

- ``intent_at`` / ``outcome_at`` separate "we asked the backend to
  create this" from "the backend confirmed it." A SIGKILL between
  the two still lets cleanup walk by deterministic backend name.
- ``metadata`` is a per-resource dict — schema-flexible. Volume kinds
  carry ``pool_backend`` so cleanup can route through ``delete_volume``;
  metadata is stamped at intent time AS WELL AS merged at confirm time,
  so a crash between the two leaves cleanup enough info to dispatch.

## Consequences

- Resume v1 (for flat plans) is a v0.x feature; the schema doesn't
  need to bump.
- Nested orchestration can store ``child_run_id`` in the metadata
  dict without a schema bump.
- The sibling ``state.pid`` file is part of this layer
  (ADR-0002 supersedes the FileLock approach).
