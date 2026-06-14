# ADR-0030: TestRange 2.0 — the build graph (DAG)

Status: Accepted
Date: 2026-06-12

**The design of record for the 2.0 cut (EPIC `DAG`).** This is a **hard break**:
2.0 deletes the v0 declarative `Plan(*hypervisors)` form factor — the frozen
`Hypervisor(vms=[...], pools=[...], networks=[...])` plus the fixed five-phase
pipeline — and rebuilds the public surface around an explicit, validated
dependency graph. There is no backward-compatibility shim; `examples/` and the
`tests/plans/` corpus are rewritten on the new API.

**Supersedes/extends** [ADR-0008](0008-driver-abc-multi-backend.md) (the driver
ABC stays, but the orchestrator drives it from a graph walk instead of a frozen
topology), [ADR-0010](0010-build-run-split.md) (the build/run split survives as
*materialize* vs *realize* per node, but the five hardcoded phases become one
topo-sorting executor), and [ADR-0021](0021-nested-virtualization.md) (nested
virtualization is re-expressed as a deferred `HypervisorNode` kind). The
numbering note below records why this is 0030 and not 0029.

> **Numbering.** This ADR was scoped in PLAN.md/TODO.md as "ADR-0029" before
> 0029 was allocated to [`rich` terminal output](0029-rich-terminal-output.md)
> (Accepted 2026-06-08). 0029 is taken, so the build graph takes the next free
> slot, **0030**. The stale "ADR-0029" references in PLAN.md/TODO.md's DAG
> section were re-pointed here (the same renumber-to-dodge-a-collision the
> nested-virt ADR did when it moved 0021→0022).

## Context

A range is a **dependency graph**, not a flat list under one host. A VM needs
its pool and its network before it can be created; a client VM needs its server
up before its tests mean anything; and — past the MVP — a vCenter appliance must
be deployed *onto* a running ESXi host, and that ESXi attached to it, before
Aria can be configured on top.

The v0 model cannot express any of that. `Orchestrator.__enter__`
(`orchestrator/runtime.py`) runs a fixed sequence — `build_phase` → `run_phase`
(itself pools → switches → sidecars → *wait-sidecars barrier* → VMs) → bind
communicators → wait ready — over a frozen
`Hypervisor(vms=…, pools=…, networks=…)` (`hypervisor.py`). The ordering is
implicit and hardcoded in the phase functions; there is no way to say "VM `web`
runs after VM `db`," no per-node caching of provisioned state, and no place for
a node kind that isn't a pool, a network, or a VM. Nested virtualization
(ADR-0021) already had to bolt a second recursive orchestrator on *after* the
outer phases (`orchestrator/nested_phase.py`) because the pipeline had no seam
for "a VM that is itself a host."

The per-build cache is the one piece that already thinks in dependencies:
`builder.config_hash` (`builders/base.py`) folds the OS-disk base image's content
sha (`base_sha`) and the build sidecar's content sha (`sidecar_sha`) into a VM's
key, so a drifted base or sidecar invalidates the cached disk-set. That is a
transitive content hash in miniature — over exactly two dependencies, hardcoded.
2.0 generalizes it.

## Decision

Make the graph first-class. A plan is constructed imperatively, frozen into a
validated `BuildGraph`, and walked by one executor.

### The model — `testrange/graph/` (pure, backend-free)

- **`Node`** (kinded ABC, `graph/node.py`) — a unit of materializable state with
  (a) a stable identity `name` (unique in its graph; the basis for the
  deterministic backend resource name and the `--resume` skip key), (b) a coarse
  `kind` tag for display/dispatch, (c) a content-addressed `cache_key`
  contract, and (d) `materialize` / `realize` hooks. MVP kinds (DAG-3):
  `PoolNode`, `NetworkNode` (the L2 fabric), `VMNode` (wraps a `VMRecipe`), and
  `SidecarNode` (a switch's DHCP/DNS/NAT sidecar). The sidecar was originally
  folded into `NetworkNode`; DAG-23 split it into its own kind — exercising this
  ADR's "new kinds, not a reshape" seam (DAG-19/20) — so the sidecar's
  storage-pool dependency attaches to the sidecar, not the L2 switch.
  *Deferred kinds, seams only:* `ApplianceNode` (deploy-through-an-endpoint) and
  `HypervisorNode` (nested — recurses an inner graph).

- **`Edge`** (`graph/edge.py`) — a directed dependency `dependent -> dependency`
  ("dependent needs dependency"). Two orthogonal facets ride on it:
  - `EdgeKind` — the semantic relationship. MVP emits one: `ORDERING`. The
    deferred relationship kinds (`manage`, `collect_from`) are new members.
  - `Cacheability` — how the edge participates in the dependent's key:
    `ORDERING` (sequencing only — contributes nothing), `BAKE` (the dependency's
    output identity is baked into the dependent's disk, so it folds into the
    key), `REPLAY` (re-applied at realize time). MVP emits only `ORDERING`;
    `BAKE`/`REPLAY` are carried so the post-MVP relationship edges and the
    transitive-key walk (DAG-5) drop in without reshaping anything.

- **`BuildGraph`** (`graph/build_graph.py`) — a frozen, validated DAG and the
  *only* thing the executor consumes. At construction it rejects duplicate node
  names, dangling dependencies, self-dependencies, and cycles (reporting a
  concrete cycle path), then precomputes the **topological waves** (Kahn by
  levels; nodes within a wave sorted by name for determinism). Validation errors
  are `GraphError` subclasses of `PlanError`, so a malformed graph flows through
  the existing invalid-plan exit-code path and `preflight` (DAG-13) with no new
  branch.

The graph is **kind-agnostic**: topology (ordering, cycle detection, teardown)
depends only on edge endpoints, never on `EdgeKind` or `Cacheability`. That
kind-agnosticism is the forward-compatibility seam — a new node or edge kind is
additive, not a reshape (validated by DAG-19/DAG-20).

The hooks take a `NodeContext`, a structural `Protocol` defined in the graph
package and left empty in the pure-model layer. The DAG executor (DAG-6) widens
it with the concrete accessors a node needs (driver, cache, state store, the
lock-guarded run ledgers). The dependency arrow points the right way: the graph
declares *what a node is handed*; the orchestrator — the one component allowed to
know both the graph and the driver — supplies an object that satisfies it,
without the graph importing anything backend-shaped (the stovepipe rule holds).

### Construction — imperative builder → frozen graph (DAG-4)

`Hypervisor()` becomes a mutable, profile-unbound node container.
`add_pool` / `add_switch` / `add_vm` each register a node *and* return its
concrete typed handle, and every added node is reachable through a typed
registry — `hyp.pools` / `hyp.networks` / `hyp.switches` / `hyp.vms`, each a
`Mapping[str, <Handle>]`. Devices and edges reference those handles, never bare
strings: `OSDrive(hyp.pools["pool1"], 16)`, `NetworkIface(hyp.networks["netA"],
…)`, `hyp.vms["web"].needs(hyp.vms["db"])`. So a miswire is a *type* error (a
network where a pool belongs won't typecheck) and a bad name is a loud
`KeyError` at construction, not a string stored for a preflight failure later.
The canonical ref form is `["name"]` (typed return + loud KeyError), **not**
attribute access `hyp.pools.pool1` — runtime-added names can't be statically
typed, so an accessor would have to type as the handle for *any* name and defeat
mypy. `Plan(name, hyp)` finalizes into the frozen `BuildGraph`. No `Any` on the
public surface.

`add_*` create nodes plus the **implicit infra edges** inferred from the handle
references (VM → its pool, VM → its network); `.needs()` adds explicit ordering
edges. The point of the explicit edge: even with plain Debian VMs and no
appliances, `hyp.vms["web"].needs(hyp.vms["db"])` makes the executor order `db`
before `web`, and the graph is real.

### The executor — one topo walk (DAG-6) replaces the phases

Topo-sort the `BuildGraph` into waves; dispatch each wave's ready nodes onto the
existing bounded I/O pool (`orchestrator/_parallel.py` — one shared, thread-safe
driver connection, never per-worker; ADR-0023) and gate the next wave on it.
`build` calls each node's `materialize` (cache-aware: build + capture the
disk-set, skippable on a hit); `run` calls `realize` (create/start, bind the
communicator, wait ready — generalizing the per-gate readiness loops, each
keeping its own independent timeout). The implicit v0 ordering (pools < switches
< sidecars < *wait barrier* < VMs; every run-VM depends on its build completing;
communicator-bind gates on VM *boot*) is exactly what the inferred infra edges
encode, so MVP wave order reproduces the v0 phase order. **Teardown is the
reverse topological order** (generalizing today's LIFO `reversed(state.resources)`),
recording each node's create/realize in `state.json` *before* the backend call
so cleanup/resume stays crash-safe (DAG-8). A per-node completion + published-
output ledger backs `--resume` (DAG-9), of which ORCH-3 becomes a thin wrapper.

### The cache — per node, transitive hash (DAG-5)

Generalize `builder.config_hash` to a **node key = hash(the node's own inputs +
the keys of its *content* dependencies)**, where a content dependency is one
reached by a cacheable (`BAKE`/`REPLAY`) edge and contributes its **output
content sha** (not its config hash, and never a truncated digest). The cached
artifact stays the existing disk-set (one OS disk + N data disks, each content-
addressed, a VM "cached" iff all roles present — `orchestrator/artifacts.py`).
Crucially, **ordering edges are not invalidation edges**: a node that merely runs
*after* another does not fold that dependency's hash into its key. MVP graphs
carry only ordering edges, so MVP node keys **match v0 keys** for equivalent VMs
(no spurious cache busting — regression-tested in DAG-5). The transitive
machinery exists from day one so post-MVP `bake` edges fold correctly.

### Inspecting the graph (first-class, for juniors — DAG-10..12)

DAGs are the conceptual cost of this model, so reading one must be trivial:
`testrange graph <plan>` renders the graph as a **dependency tree** — each
final target with everything it is built from nested beneath it (a shared
sub-tree is expanded once then back-referenced), mirroring the `describe` tree
idiom. `graph --order` shows the topo waves (what runs in parallel, in what
order), `graph --dot` emits Graphviz, and `graph --cache --profile <p>`
annotates each node with its key + hit/miss. `preflight` validates the graph
(cycles, dangling deps, duplicate names) before any backend call.

The original cut also shipped a `why <plan> <node>` command for a single node's
dependencies/dependents/wave; it was removed (DAG-21/22, 2026-06-14) once the
default `graph` tree made the same relationships visible at a glance.

### MVP scope and the deferred seams

**In:** libvirt backend only; `CloudInitBuilder` / Debian VMs; ordering edges;
the builder API, the executor, the per-node cache, and the inspection commands.

**Out (seams reserved, explicitly NOT built):** appliances /
deploy-through-an-endpoint (`ApplianceNode`); relationship edges with bake/replay
(`manage`, `collect_from`); nested hypervisors (`HypervisorNode` recursing an
inner graph); backends other than libvirt. The `Node`/`Edge` ABCs, the edge
cacheability field, the transitive-hash key, and the kind-agnostic executor are
designed so these land as **new node/edge kinds, not a reshape**. The end state
remains nested hypervisors running appliances across backends; the MVP just
doesn't wire them. Forward-compat is held by the DAG-19 (appliance + relationship
edges) and DAG-20 (`HypervisorNode` + second backend) seam-check tickets.

## Consequences

- The v0 `Plan(*hypervisors)` surface, the frozen `Hypervisor` kwargs, and the
  hardcoded phase modules are deleted (DAG-14); there is no deprecation path.
  Every example and every `tests/plans/` entry is rebuilt on the builder API,
  and libvirt is re-certified through `testrange run` on the new surface
  (DAG-15/16). The unit suite drives the executor against `MockDriver` (DAG-17).
- A range gains inter-node ordering (`.needs()`) and the model gains a place for
  appliances and nested hosts — the things the fixed pipeline structurally could
  not hold.
- ADR-0008's driver ABC is unchanged; the orchestrator simply calls it from node
  `materialize`/`realize` hooks instead of phase functions. ADR-0010's build/run
  split survives as materialize/realize. ADR-0023's parallelism substrate (one
  shared connection, the `--jobs` cap, the ledger locks) is reused verbatim by
  the wave dispatcher. ADR-0021's nesting becomes a deferred node kind rather
  than a bolted-on recursive phase.
- The pure-model layer (`testrange/graph/`, DAG-2) lands first and standalone:
  it imports no driver and is fully unit-tested without a backend, so the
  foundation is mergeable and gate-green before any of the backend-coupled
  rewiring begins.

## Addendum — `hyp.vm()` hardware façade (CORE-101, 2026-06-12)

The construction surface above proved verbose in practice:
`add_vm(VMRecipe(spec=VMSpec(devices=[…]), builder=…, communicator=…))` is three
mandatory wrapper layers, and the singleton devices (CPU/Memory/OSDrive) sit in
the same flat `devices=[]` list as the zero-or-more NICs and data disks. We add
**one** method — `Hypervisor.vm(name, *, cpu, memory, os_drive, builder,
communicator, nics=(), data_disks=(), firmware="bios") -> VMHandle` — as sugar
over the unchanged `add_vm(VMRecipe(VMSpec(...)))`. Chosen via a design
judge-panel; the load-bearing claims were verified against source.

- **Pure sugar, byte-identical keys.** `vm()` packs `[cpu, memory, os_drive,
  *data_disks, *nics]` into a `VMSpec` and delegates to `add_vm`. `config_hash`
  depends only on the relative order of NICs and of data disks (`spec.nics` /
  `spec.data_drives`, and `macs` in spec order), both preserved — so the cache
  key (DAG-5 parity) is unchanged. The explicit `add_vm(VMRecipe)` form stays
  public as the escape door.
- **Device objects, not primitives.** `os_drive` / `data_disks` / `nics` are
  typed as the generic device classes, so a backend's concrete subclasses (an
  `OSDrive` subclass carrying extra knobs) fit the same slots through this one
  method. A primitive `os_drive=int` would have hardcoded the generic `OSDrive`
  and locked the backend-concrete corpus out.
- **Structural singletons.** Naming `cpu` / `memory` / `os_drive` makes the
  "exactly one of each" rule a signature property — `memory=OSDrive(…)` is a
  mypy error — stricter than the explicit path's runtime arity check (which
  remains the backstop for that path).
- **Stovepipes untouched.** `builder` and `communicator` are forwarded into the
  unchanged three-slot `VMRecipe`; the façade learns nothing about either.
- **Rejected.** A bare-string address shortcut (`Nic(net, "10.0.0.5")`): a
  handle is a `str` subclass and `StaticAddr.__post_init__` accepts any
  IP-shaped string, so the shortcut would let an IP-named handle type-check — a
  verified hole. `Nic()` / `OS()` / `Disk()` free-function aliases (dual-spelling,
  no weight pulled). Widening `add_pool` to `(name, size)` (softens an existing
  `TypeError`).

`examples/` and the portable `tests/plans/generic/` corpus move to `hyp.vm()`;
the backend-concrete plans (`tests/plans/{libvirt,proxmox,esxi}/`) and the
parameterized-recipe-helper plans (`concurrency.py`, `sidecar_flags.py`) stay on
the explicit form as the escape-door example.
