# Build vs run

`testrange` splits a range's lifecycle into two independent phases, exposed as
two CLI verbs over the same plan ([ADR-0010](../adr/0010-build-run-split.md)).
Both verbs are walks of the plan's frozen build graph by one topo-sorting
executor ([thinking in build graphs](thinking-in-build-graphs.md)) — `build`
runs each node's *materialize* step, `run` its *realize* step:

- **`testrange build <plan>`** — materialize every node: provision every VM to
  completion and capture its disks into the cache. Runs **no** tests and
  creates no run VMs. Ordering edges (`.needs()`) do **not** gate builds —
  they sequence the run, not the disks — so all VMs build concurrently.
- **`testrange run <plan>`** — realize the graph wave by wave from cached
  disks and run the plan's `TESTS`.

```sh
testrange build examples/hello_world.py   # warm the cache; exits when done
testrange run   examples/hello_world.py    # pure warm-cache bring-up + tests
```

## What `build` does

For each VM, `build` resolves the base image, computes a deterministic
`config_hash`, and probes the cache for the VM's full **disk set** — the OS
disk plus every data disk, each stored under its own name
(`_built_<hash>__os`, `_built_<hash>__data0`, …). A VM is cached only when
**all** its artifacts are present; a partial set is a miss for the whole VM.

Only if at least one VM misses does `build` stand up its ephemeral
infrastructure (a dedicated build pool, a transient internet-connected build
switch, and a sidecar) and build **only the missing VMs**. Each is booted with
all its writable disks attached; the install payload populates them; on
power-off every disk is captured into the cache. When the build finishes the
backend is empty again — build VMs, disks, pool, switch, and sidecar are all
torn down. A 100%-cache-hit `build` touches the backend not at all.

When an HTTP cache is configured (`--cache <url>`), each captured disk is also
pushed upstream, which is what makes `build` a build-farm primitive: one host
warms a shared cache that many `run` invocations consume.

Caching is **per graph node**, and a VM node's key is the same deterministic
`config_hash` as before 2.0 — existing caches stay valid. A `.needs()`
ordering edge never folds into a key (placement is not invalidation).
`testrange graph <plan> --cache --profile <name>` previews each node's
hit/miss without touching the backend.

## What `run` does

`run` brings the range up from the cache, realizing the graph wave by wave:
pools first, then switches (a switch's wave completes only once its sidecar is
serving), then VMs — each VM's cached disks pushed onto its own volumes, the VM
started, its communicator bound, and its builder's readiness check passed
before any node that depends on it proceeds. A `web.needs(db)` edge therefore
means `web` boots only after `db` is genuinely ready, not merely created. Once
the whole graph is realized, `run` executes `TESTS`.

`run` **auto-builds** anything not yet cached — it runs the build phase over the
missing VMs first, so a cold cache just works:

```sh
testrange run examples/hello_world.py     # builds what's missing, then runs
```

For CI that wants build and run as distinct, auditable steps, `--require-cache`
makes a miss **fail fast** instead of building:

```sh
testrange build examples/hello_world.py            # step 1: warm the cache
testrange run --require-cache examples/hello_world.py   # step 2: never builds
```

A `run --require-cache` against a cold cache exits non-zero with a "run
`testrange build` first" message and creates nothing.

## Why split it

- **Cache warming is decoupled from testing.** A build farm can warm a shared
  HTTP cache without running anyone's tests.
- **Failure is resumable.** Each VM's disks land in the cache before the next VM
  builds, so a build that dies on VM 3 of 5 leaves 1–2 cached; the next
  `build` skips them via its up-front cache probe and rebuilds only 3–5. A
  dead `run` is resumable too — see below.
- **The backend is pure scratch space.** Nothing testrange-owned survives
  between phases — no shared base images, no overlays, no leftover pools.

## One run at a time

A plan's run is owned by a single `testrange` process from build through
teardown. Running two `testrange` invocations against the **same** plan
concurrently is unsupported: a run's `state.json` is written only by its
owning process, and `testrange cleanup` refuses to touch a run whose owning
PID is still alive (it reports `PID <X> still alive`). There is no
cross-process lock beyond that liveness check.

A run whose owning process **died** can be continued rather than torn down:
`testrange run --resume <run_id> <plan>` takes ownership, skips every graph
node the run's ledger records as completed, reattaches to the still-live
resources, and carries on from the first incomplete node.

To run things in parallel, use **separate** plans — each gets its own
`run_id`, state directory, and backend resources.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (build warmed / all tests passed) |
| 1 | a test failed (`run`) or the build failed |
| 2 | preflight failed, or `run --require-cache` hit a cache miss |
| 3 | cleanup failed |
