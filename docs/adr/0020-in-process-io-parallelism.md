# ADR-0020: Bounded in-process parallelism in the orchestration I/O phases

Status: Accepted
Date: 2026-05-31

**Amends [ADR-0002](0002-no-asyncio.md)** — that ADR made each run sync and
single-threaded ("install brings up one VM at a time… No asyncio, no
ThreadPoolExecutor"). This ADR narrows that to: the *public API* stays
synchronous and *test execution* stays sequential, but the orchestration **I/O
phases** may use a bounded thread pool internally. **Does not touch
[ADR-0018](0018-single-instance-only.md)** — this is intra-process parallelism
under one owner; multi-instance concurrency remains unsupported.

## Context

A single `testrange run` against a multi-VM range is almost entirely I/O wait:
per-VM disk uploads into the pool, multi-GB build-disk capture downloads, and
per-VM/per-sidecar readiness polls all block the main thread and run serially.
N VMs each waiting ~boot-time are *added*, not overlapped. ADR-0002's blanket
"no ThreadPoolExecutor" was written when the only concern was a single VM's
bring-up; it has become the thing standing between the tool and an obvious
wall-clock win that costs nothing semantically — the work items are independent
and the slow calls are blocking C/socket I/O that release the GIL.

The naive blocker is that the per-call clients look un-thread-safe. But the
*operations the phases actually use* are concurrency-tolerant on one connection:
libvirt's `virConnect` is internally locked and serves concurrent `virStream`
uploads/downloads; proxmoxer's `requests.Session` and paramiko's transport
multiplex concurrent requests/SFTP channels. What is **not** shareable is a
*second* connection: each driver caches cross-call resource-name resolution in
per-instance maps — libvirt `_libvirt_net_by_network`, proxmox `_vnet_by_network`
plus a random per-instance `_sdn_zone` — so a network created on one connection
is unresolvable on another. (An earlier draft used per-worker connections and
the real-libvirt smoke test caught exactly this: a build VM created on a worker
connection couldn't find the build network created on the main connection.)

## Decision

**The orchestration I/O phases run on a bounded thread pool driving the one
shared, thread-safe driver connection. Test execution does not.** Three rules
make it safe:

1. **One shared connection, driven concurrently.** All workers use
   `ctx.driver`. Its in-memory resource maps stay consistent (one instance), and
   the slow transfers overlap because the underlying clients serve concurrent
   streams/requests. Per-worker connections were rejected (see Context).

2. **Agent commands serialize on a per-driver `call_lock`.** The native guest
   channel (QGA over libvirt / the PVE agent REST) issues commands on the shared
   connection; a brief re-entrant lock around each command keeps concurrent
   readiness polls from interleaving on it, while the polls' sleeps stay outside
   the lock so the waits still overlap.

3. **Shared bookkeeping behind a mutex, held only for the quick mutation.**
   `StateStore`'s RMW pairs (`record_intent`/`confirm`/`forget`/`set_phase`) take
   an in-process lock so concurrent workers can't clobber one another's ledger
   additions; the `RunContext` resource dicts take `ctx.ledger_lock`. Both are
   released across the slow backend call.

Supporting changes that this unblocks/requires:

- A single `parallel_map` helper owns the worker cap (`--jobs`, default
  `DEFAULT_MAX_WORKERS=8`) and the fail-fast policy (first worker exception, in
  submission order, propagates; pending workers are cancelled).
- The local cache's fixed-name staging paths all move to per-write
  `tempfile.mkstemp` (CACHE-4): the URL download, the `.bin` copy, and the
  `.json` sidecar write. Two concurrent adds of the *same* content sha would
  otherwise collide on `<sha>.bin.partial` / `<sha>.json.partial`. A small
  in-process write lock additionally serializes the sidecar read-modify-write so
  concurrent same-sha adds *merge* their name aliases instead of clobbering; the
  slow byte copy stays outside it, so distinct-content adds parallelize fully.
  The HTTP-tier `fetch` already used `mkstemp` (CACHE-3).
- Parallel build needs a **distinct, deterministic** build NIC IP per in-flight
  VM, allocated as a stable function of the VM (ADR-0017 widening): scheduling
  order must not perturb `config_hash`.

What stays serial, deliberately:

- **Test execution** — user code sharing one range; §14's sequential,
  continue-on-failure contract is unchanged.
- **Teardown of the build infra** — strict LIFO (sidecar → networks → switch →
  pool).
- The **public API** — no `async def` anywhere; threads are an internal
  implementation detail of the phases.

## Consequences

- ADR-0002's "no ThreadPoolExecutor" is superseded *for the I/O phases only*;
  its sync-public-API and sequential-tests guarantees stand.
- A multi-VM run overlaps its uploads, downloads, and readiness waits, bounded
  by `--jobs`. Wall-clock for the I/O phases drops from ~Σ to ~max of the
  independent work.
- The thread-safety substrate (StateStore lock, `ctx.ledger_lock`, the per-driver
  `call_lock`, the cache `mkstemp` + alias-merge fix) is a permanent invariant:
  any new shared mutable state touched inside a phase must be guarded the same
  way, and any new driver must keep its connection usable from several threads.
- ADR-0018 is untouched. The state/cache *cross-process* story is still "single
  instance"; the new in-process lock is a finer-grained, complementary guard,
  not a relaxation of that contract.
