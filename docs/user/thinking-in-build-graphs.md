# Thinking in build graphs

Since TestRange 2.0, a plan is not a list of VMs under a host — it is a
**dependency graph**. You register pools, switches, and VMs on a `Hypervisor`;
every reference between them goes through a **typed handle**; and `Plan(...)`
freezes the result into a validated graph that one executor walks. This page
is the conceptual on-ramp: what the graph is, where its edges come from, and
how to read it from the CLI without reading code.

## From plan to graph

```python
hyp = Hypervisor()
hyp.add_pool(StoragePool("pool1", 32))
hyp.add_switch(Switch("switch1", Network("netA"), cidr="172.31.0.0/24",
                      sidecar=Sidecar(dhcp=True, dns=True)))

db = hyp.add_vm(VMRecipe(
    spec=VMSpec(name="db", devices=[CPU(2), Memory(2048),
                                    OSDrive(hyp.pools["pool1"], 16),
                                    NetworkIface(hyp.networks["netA"],
                                                 addr=StaticAddr("172.31.0.110"))]),
    builder=CloudInitBuilder(base=CacheEntry("debian-13"),
                             credentials=[PosixCred("u", password="p", admin=True)],
                             packages=[Apt("postgresql")]),
    communicator=SSHCommunicator("u"),
))
web = hyp.add_vm(VMRecipe(...))   # same shape, nginx instead

web.needs(db)                     # explicit: web runs only after db is READY

PLAN = Plan("two-tier", hyp)
```

Three things to internalize:

1. **`add_*` returns a typed handle, and handles are the only way to refer to
   a registered node.** `OSDrive` takes a `PoolHandle`, `NetworkIface` takes a
   `NetworkHandle`, `.needs()` takes handles. Pass a bare string (or the wrong
   handle kind) and mypy rejects it — and so does the constructor at runtime.
   A typo'd name (`hyp.pools["pool11"]`) is a loud `KeyError` *at
   construction*, listing the names that do exist.
2. **Every handle reference becomes an edge.** A VM that puts its OS drive in
   `pool1` *depends on* `pool1`; a NIC on `netA` makes the VM depend on the
   switch that carries `netA`. You never declare those edges — the references
   are the edges. `.needs()` adds the ones the executor cannot infer: "web is
   only meaningful after db is up."
3. **`Plan(name, hyp)` freezes everything.** The container seals (later
   `add_*` calls raise), the whole topology is validated, and graph defects —
   duplicate names, a reference to a node that was never registered, a
   `.needs()` cycle — fail right there, before anything touches a backend.

## What the executor does with it

The graph is sorted into **waves**: wave 0 is every node with no
dependencies, wave 1 is everything whose dependencies are all in wave 0, and
so on. Nodes *within* a wave run in parallel; the next wave starts when the
previous one is done. For the two-tier plan above:

```text
wave 0: pool:pool1            # pools first
wave 1: network:switch1       # the switch's sidecar disk lives in pool1
wave 2: vm:db                 # needs its pool + its network
wave 3: vm:web                # .needs(db) pushed it one wave later
```

A node is not "done" when it is created — it is done when it is **ready**.
A network node's turn ends only when its sidecar answers and is serving
DHCP/DNS; a VM node's turn ends only when its communicator answers, its DHCP
leases exist, and its builder's readiness probe passes. So `web.needs(db)`
means web *boots* after db is genuinely reachable, not merely created.

Two walks share this machinery:

- **`testrange build`** runs every node's *materialize* step — for VMs, build
  the disk set or hit the cache. Ordering edges do **not** gate builds (they
  sequence the run, not the disks), so all your VMs build concurrently.
- **`testrange run`** materializes anything missing, then runs every node's
  *realize* step wave by wave, then executes your `TESTS`.

Teardown is the reverse: every backend resource was recorded before it was
created, in wave order, so cleanup unwinds the graph back-to-front.

## Reading a graph from the CLI

You never have to derive any of the above by hand:

```sh
testrange graph plan.py            # the dependency tree: each target with what it's built from
testrange graph plan.py --order    # the waves: what runs, in what order, in parallel
testrange graph plan.py --dot      # Graphviz (pipe into `dot -Tsvg`)
testrange graph plan.py --cache --profile lab
                                   # + each node's cache key and hit/miss:
                                   #   what would clone vs what would build
```

The default `graph` view is a tree rooted at each final target (the nodes
nothing else depends on), with everything that target is built from nested
beneath it. That answers the two questions you will actually ask while
debugging a plan: *why does this run so late?* — read its dependencies down
the branch — and *what depends on it?* — every place the node appears nested.

`preflight` validates the same graph (plus backend checks) without creating
anything, and `describe` shows a one-line graph summary next to the topology.

## Node names

Graph node names are kind-qualified so they can never collide across kinds:
`pool:pool1`, `network:switch1` (one node per switch — its networks, fabric,
and sidecar realize as a unit), `vm:web`. Those are the names the `graph` tree
prints on every line.

## Caching in one paragraph

Every node has a content-addressed **cache key**. For a VM it is the same
deterministic hash of its build inputs (base image, packages, addressing,
MACs...) that keyed the cache before 2.0 — your existing cache stays valid. A
node that merely runs *after* another does not fold that node into its key:
**placement is not invalidation**. The edge kinds that *do* fold a
dependency's identity into a dependent's disk (`bake`/`replay`) are reserved
for the appliance work; today every edge is an ordering edge and
`graph --cache` will show you exactly what a run would clone versus build.

## Common errors, decoded

| You see | It means |
| --- | --- |
| `no pool 'pool11'; known: pool1` | Typo'd registry lookup at construction. |
| `OSDrive.pool must be a PoolHandle ...` | A bare string reached a device; use `hyp.pools["..."]`. |
| `cannot add_pool: this Hypervisor was frozen by Plan(...)` | Register everything before `Plan(...)`. |
| `edge 'vm:web' -> 'pool:x' names unknown node` | A handle from another container (or hand-minted) references a node this plan never registered. |
| `build graph 'x' has a dependency cycle: vm:a -> vm:b -> vm:a` | Your `.needs()` edges loop; no order can satisfy them. |
