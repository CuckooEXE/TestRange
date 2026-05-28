# ADR-0015: The Plan entry is topology; the backend is bound separately at run time

Status: Accepted
Date: 2026-05-27

Relates to [ADR-0008](0008-driver-abc-multi-backend.md) (the driver ABC this
binds a plan to), [ADR-0010](0010-build-run-split.md) (build/run phases the
binding feeds), and [ADR-0014](0014-managed-build-switch.md) (the build switch
that moves onto the binding).

## Context

Every `Plan` entry was a concrete `*Hypervisor` (`ProxmoxHypervisor`,
`MockHypervisor`, `LibvirtHypervisor`). That single type conflated four jobs:

1. **topology** — the networks/pools/vms the test declares;
2. **backend selection** — its Python type drove `driver_for(type)`;
3. **connection** — host / user / password / port / node / ssh;
4. **environment knobs** — build egress, backing storage, node.

Jobs 2–4 forced a portable test to hard-code one backend and to carry a host
address and a password in the committed plan. The topology layer (job 1) was
already 100% backend-agnostic — no backend-specific device / builder /
communicator / network / vm subclasses exist — so the conflation, not the
topology, was the only thing pinning a test to a backend.

## Decision

Split the Plan entry from the backend binding.

- A new generic **`Hypervisor`** (`from testrange import Hypervisor`) carries
  **only** portable topology (networks/pools/vms). It selects no driver — it is
  deliberately *not* registered in the driver registry — and carries no
  connection. This is the entry a portable plan uses.

- A concrete **`*Hypervisor`** still exists and still **pins** its driver. Use
  it when a test genuinely needs a specific backend.

- The backend is supplied at run time by a **connection profile**: a local TOML
  file passed with `--connect <profile>`. It names the driver by a short
  *scheme* (`driver = "proxmox"`), carries the connection, and carries the
  build-egress env-knob. Secrets policy is deliberately simple — passwords live
  inline as plain strings; TestRange backends are firewalled lab environments,
  and `.gitignore` keeps a real profile out of git. There is no env/file secret
  indirection and no `TESTRANGE_CONNECT` environment fallback; `--connect` is the
  only knob, so an invocation is fully self-describing.

- `resolve_backend(plan, profile)` folds the entry and the optional profile into
  a single `ResolvedBackend { driver, build_switch, driver_uri }` the
  orchestrator consumes. It enforces the matrix:

  | (entry, profile)  | resolution |
  | ----------------- | ---------- |
  | concrete + none   | today's path: driver from the entry's type; build egress + URI from the entry (full back-compat). |
  | concrete + given  | the profile's `driver` scheme **must** equal the entry's scheme, else a hard error; the driver is built from the profile connection; build egress from the profile; topology stays the entry's. A concrete entry pins the driver — a profile may override the *connection only*. |
  | generic + none    | hard error: the plan is backend-agnostic; pass `--connect`. |
  | generic + given   | driver from the profile scheme; build egress from the profile. |

- **Build egress lives on the binding, not the topology.** Following
  [ADR-0014](0014-managed-build-switch.md), the build switch is the binding's
  env-knob. A concrete entry still declares it inline; a profile expresses the
  managed-egress form with a `[build_switch]` table mapping to
  `ManagedBuildSwitch(uplink, cidr)`. A bring-your-own plain-`Switch` egress path
  is not expressible in a profile by design — declare it by pinning the plan.

### Compatibility preflight — three layers

1. **pin / driver-match** — the static matrix above, raised in
   `resolve_backend` at construction.
2. **portability lint** — `compatibility_findings(plan, driver)`, a near-empty
   honest hook today (the topology is backend-agnostic, so nothing to reject);
   it is the seam for the day a backend-specific device subclass declares which
   drivers realize it.
3. **live capability findings** — the resolved driver's own `preflight` (mgmt
   gating, managed-egress capability). The orchestrator merges layers 2 and 3.

## Consequences

- A portable plan (`from testrange import Hypervisor`) runs unmodified against
  any backend; the address and password move out of the committed test into a
  local, gitignored profile. `examples/hello_world.py` is now this portable
  form; `examples/px_hello.py` remains the pinned-Proxmox example.
- `testrange describe` prints the resolved backend binding (driver, host, port,
  node, build egress) with the **password masked** — describe output is the most
  likely thing to be pasted into a report. A generic plan with no `--connect`
  renders its topology and reports `backend: UNBOUND`.
- Concrete plans keep working with no profile (full back-compat), so the split
  is additive.
- The driver registry gains a scheme map (`driver_for_profile`) and pin
  introspection (`scheme_for_hypervisor`, `is_pinned`) alongside the existing
  type and name dispatch.
