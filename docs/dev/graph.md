# The build graph (DAG)

This is the developer-facing tour of TestRange's core data structure: the
**build graph**, a directed acyclic graph (DAG) of everything a plan has to
create. It is the design of record for the 2.0 engine (ADR-0030); the
user-facing companion, [Thinking in build graphs](../user/thinking-in-build-graphs.md),
covers the same ground from a plan author's seat. Read this one if you are
working *on* the orchestrator rather than *with* it.

## Why a graph at all

TestRange stands up little fleets — VMs wired to storage pools, virtual
switches, and networks. Everything in that world depends on something else:

- a VM's disk lives in a **storage pool**, so the pool must exist first;
- a VM's NIC plugs into a **switch**, so the switch must exist first;
- sometimes a VM must boot *after* another is genuinely ready (its first-boot
  script phones home to a database).

The pre-2.0 engine solved that ordering with **fixed phases**: build all pools,
then all switches, then all VMs, then run — with a global barrier between each.
That worked until it didn't:

- **Rigid.** Every new kind of dependency meant surgery on the phase pipeline.
- **Over-synchronized.** Every VM waited for every *other* VM at each barrier,
  even with nothing in common.
- **Implicit.** The order was buried in control flow; nobody could *see* why a
  thing happened when it did.
- **Caching-hostile.** "What rebuilds if I change this?" has no clean answer
  when dependencies aren't first-class data.

A DAG fixes all of that by turning the dependencies into **data** instead of
**control flow**. The order stops being hand-written and starts being *derived*.

## What a DAG is

Three words, each load-bearing:

- **Graph** — nodes (things) joined by edges (relationships). Here: pools,
  switches, and VMs joined by "depends on".
- **Directed** — edges point. "`vm:web` depends on `pool:p`" is an arrow
  `web → p`. Direction is what encodes ordering.
- **Acyclic** — no cycles. If A→B→C→A, there's no valid order to build in;
  forbidding cycles is exactly what *guarantees* a build order exists.

The payoff property: any DAG can be **topologically sorted** — flattened into
waves where everything comes after the things it depends on. That sort *is* the
build plan, computed from the structure rather than written by hand.

## How TestRange builds and walks one

Three layers: how a plan author *describes* the graph, what the graph *is*, and
what *walks* it.

### Construction — references, not strings

You build a mutable `Hypervisor`; every `add_*` hands back a typed **handle**,
and devices are wired with those handles:

```python
hyp = Hypervisor()
pool1 = hyp.add_pool(StoragePool("pool1", 32))      # -> PoolHandle
netA  = hyp.networks["netA"]                         # -> NetworkHandle

db  = hyp.vm("db",  cpu=CPU(2), memory=Memory(2048), os_drive=OSDrive(pool1, 8), ...)
web = hyp.vm("web", cpu=CPU(2), memory=Memory(1024), os_drive=OSDrive(pool1, 8),
             nics=[NetworkIface(netA, StaticAddr("172.31.0.150"))], ...)
web.needs(db)                                         # explicit ordering edge
```

The load-bearing trick lives in `testrange/handles.py`: **handles are `str`
subclasses.** A `PoolHandle` *is* the pool's name `"pool1"` — just typed. That
buys two things at once:

- **Miswiring is a compile-time error.** `OSDrive` takes a `PoolHandle`; hand it
  a bare string or the wrong handle kind and mypy rejects it and the runtime
  raises `TypeError`. You can't reference a pool that doesn't exist — there is
  no handle for it. (This is the "loud failure for juniors" the design insisted
  on: the graph cannot be silently wrong.)
- **Downstream stays byte-identical.** Because a handle still *is* its string,
  every name consumer — drivers, the sidecar's DNS records, the cache-key hasher
  — reads names exactly as before. The move to a graph changed *zero* bytes of
  what gets hashed (regression-pinned as the v0 key-parity test).

`Plan(name, hyp)` then **validates** (cycles, dangling refs, duplicate names),
**freezes** the builder, and produces the immutable `BuildGraph`
(`testrange/graph/build_graph.py`). After that, further `add_*` / `vm()` calls
raise.

### The graph — kinded nodes, two flavors of edge

Each resource becomes a `Node` (`testrange/graph/node.py`, concretes in
`testrange/nodes.py`): `PoolNode`, `NetworkNode`, `VMNode`. Names are
kind-qualified so they can never collide — `pool:p`, `network:<switch>` (one
node per switch: its networks, fabric, and sidecar realize as a unit), `vm:web`.
A node carries an identity, a kind tag, a `cache_key()`, and the
`materialize` / `realize` hooks.

Edges (`testrange/graph/edge.py`) come in two flavors, and the distinction is
the whole game:

- **Content edges** ("built *from*") — `vm:web → pool:p`. They affect *what gets
  built* and fold into the cache key.
- **Ordering edges** (`.needs()`) — `vm:web → vm:db`. They affect *when* a node
  runs but **not what it is**. db being ready doesn't change web's disk by one
  byte.

The graph itself is **kind-agnostic**: topology depends only on edge endpoints,
never on edge or node kind. That is what lets the deferred kinds (appliances,
nested hypervisors, `bake`/`replay` relationship edges) drop in as *new
subclasses* rather than a reshape.

### The executor — one walk

`testrange/orchestrator/executor.py` replaces the entire phase pipeline. It
topo-sorts, then:

1. **Materialize** over *content* waves — build/cache each node's artifact.
   Ordering edges are excluded here, so unrelated VMs still build fully in
   parallel (the old concurrency, preserved by construction). MVP graphs carry
   only ordering edges, so this is a single wave.
2. **Realize** over *full* waves — create and power on, with per-node readiness
   gates folded in. The old global barriers became per-node gates, so
   `.needs(db)` now means "after db is *genuinely* ready", not "after an
   arbitrary phase boundary".

Teardown walks the waves in reverse: every backend resource was recorded before
it was created, so cleanup unwinds the graph back-to-front.

### Cache keys — the transitive hash

Every node has a content-addressed key (`testrange/graph/keys.py`):
`hash(this node's own inputs + the keys of its *content* dependencies)`. A
serial topo walk routes each dependency's key into the next node's
`cache_key()`. Because the key is contextual — a base image's content sha and
the deterministic NIC MACs come from the bound driver and cache — `graph
--cache` requires `--profile`. Ordering edges are excluded: **placement is not
invalidation**. For a VM the result is byte-identical to the pre-2.0
`config_hash`, so existing caches stay valid.

## What it affords

- **Correct-by-construction wiring** — typed handles make a malformed range a
  type error, not a confusing runtime explosion three minutes into a build.
- **Principled caching** — the transitive hash rebuilds exactly the nodes
  downstream of a change and hits the cache for everything else.
- **`--resume`** — a per-node completion ledger lets an interrupted run reattach
  and skip finished work.
- **Inspectability** — `testrange graph <plan>` (dependency tree),
  `graph --order` (waves), `graph --dot` (Graphviz), `graph --cache --profile`
  (per-node hit/miss). The structure is never a mystery.
- **Free, correct parallelism** — waves come from the topology, so maximum safe
  concurrency needs no hand-tuning.
- **Extensibility without reshape** — new capabilities land as new node/edge
  *kinds* against the same executor, not as new phases.

The one-sentence model to hand a new teammate: *the old engine wrote the build
order by hand; this one writes down the dependencies and lets the order be
computed* — the same reason you'd rather declare a Makefile than write a build
script full of `sleep`s.

## Reading the graph from the CLI

```sh
testrange graph plan.py            # the dependency tree (default)
testrange graph plan.py --order    # the execution waves
testrange graph plan.py --dot      # Graphviz (pipe into `dot -Tsvg`)
testrange graph plan.py --cache --profile lab   # + per-node cache key / hit-miss
```

The default view is a tree rooted at each **sink** node — a plan's final
targets, the ones nothing else depends on — with everything that target is built
from nested beneath it. A DAG is not a tree (a shared dependency has several
dependents), so a sub-tree is expanded in full the first time it is reached and
shown as a back-reference (`⤴ (shown above)`) afterward, keeping the render
linear in the graph:

```
multi-tier-app: 5 node(s), 7 edge(s), 3 wave(s)
└── vm:web
    ├── network:backend
    ├── network:edge
    │   └── pool:pool1
    ├── pool:pool1
    └── vm:db
        ├── network:backend
        └── pool:pool1
```

(An earlier `why <plan> <node>` command reported a single node's
dependencies/dependents/wave; it was removed once this tree made the same
relationships visible at a glance — DAG-21/22.)

## Where we are, and what's left

The DAG cut is **complete** (EPIC `DAG`, ADR-0030): typed handles → frozen
`BuildGraph`, one topo-sorting executor, the transitive-hash key with proven v0
parity, the `graph` inspection surface, and `--resume`. The pre-2.0 form factor
(frozen `Hypervisor` kwargs, `Plan(*hypervisors)`, the phase modules) is deleted
— a hard break, no shim.

What remains is the work the MVP deliberately deferred, designed *for* but not
built:

- **New node/edge kinds** — nested hypervisors return as a `HypervisorNode`;
  appliances (deploy-through-an-endpoint); relationship edges (manage /
  collect-from with `bake`-vs-`replay` caching). The seams are pinned by tests
  so these land as additions.
- **Multi-backend certification** — the MVP walks libvirt; the Proxmox and ESXi
  drivers are certified against the same graph corpus under their own epics,
  feeding the 1.0.0 release gate.

## Map of the code

| Concern | Module |
| --- | --- |
| Typed `str`-subclass handles | `testrange/handles.py` |
| Concrete node kinds | `testrange/nodes.py` |
| Node ABC + context protocol | `testrange/graph/node.py` |
| Edge + cacheability | `testrange/graph/edge.py` |
| Frozen, validated DAG + waves | `testrange/graph/build_graph.py` |
| Transitive cache-key walk | `testrange/graph/keys.py` |
| The one executor | `testrange/orchestrator/executor.py` |
| Per-resource build / run machinery | `testrange/orchestrator/vm_build.py`, `vm_run.py` |

See [ADR-0030](../adr/0030-build-graph-dag.md) for the decision record and
`PLAN.md` for the living design.
