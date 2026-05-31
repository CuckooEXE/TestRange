# Build vs run

`testrange` splits a range's lifecycle into two independent phases, exposed as
two CLI verbs over the same plan ([ADR-0010](../adr/0010-build-run-split.md)):

- **`testrange build <plan>`** — provision every VM to completion and capture
  its disks into the cache. Runs **no** tests and creates no run VMs.
- **`testrange run <plan>`** — bring the range up from cached disks and run
  the plan's `TESTS`.

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

## What `run` does

`run` brings the range up from the cache: it creates the user's pools, switches,
and sidecars, waits for each sidecar to be serving, then pushes each VM's cached
disks onto its own volumes and starts it. Then it binds communicators, waits for
each builder's readiness check, and executes `TESTS`.

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
  `build` skips them via its up-front cache probe and rebuilds only 3–5.
- **The backend is pure scratch space.** Nothing testrange-owned survives
  between phases — no shared base images, no overlays, no leftover pools.

## One run at a time

A plan's run is owned by a single `testrange` process from build through
teardown. Running two `testrange` invocations against the **same** plan
concurrently is unsupported: a run's `state.json` is written only by its
owning process, and `testrange cleanup` refuses to touch a run whose owning
PID is still alive (it reports `PID <X> still alive`). There is no
cross-process lock beyond that liveness check.

To run things in parallel, use **separate** plans — each gets its own
`run_id`, state directory, and backend resources.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (build warmed / all tests passed) |
| 1 | a test failed (`run`) or the build failed |
| 2 | preflight failed, or `run --require-cache` hit a cache miss |
| 3 | cleanup failed |
