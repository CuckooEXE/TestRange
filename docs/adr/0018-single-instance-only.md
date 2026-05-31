# ADR-0018: Single-instance-only operation; multi-instance concurrency deferred

Status: Accepted
Date: 2026-05-31

**Extends [ADR-0002](0002-no-asyncio.md)** — that ADR established a sync,
single-threaded process model *within* one run. This ADR states the
complementary external contract: only one `testrange` process runs at a time
for a given user/profile, and reframes the state/cache crash-safety machinery
as exactly that — crash safety, not a concurrency mechanism.

## Context

ADR-0002 made each run single-threaded internally. But the docstrings on
`StateStore`, `LocalCache`, `CacheManager`, and `HttpCache`, plus PLAN.md §16,
had drifted into language that *read* like concurrency guarantees: "atomic
read/write", "not thread-safe but…", PID-first ordering justified against "a
concurrent `cleanup --all`". A reader could reasonably conclude two `testrange`
invocations against the same cache, state root, or driver profile were a
supported, guarded workload. They are not.

Concretely, nothing in the codebase serializes two live processes:

- The cache stages downloads to **fixed-name** `.partial` paths
  (`cache/local.py`, `cache/http.py`) — two concurrent fetches interleave into
  one file, then each promotes a disk whose bytes don't hash to its name.
- `StateStore`'s read-modify-write pairs (`record_intent`/`confirm`/`forget`/
  `set_phase`) are atomic *per write*, not *per pair* — two writers lose updates.
- Backend resource names are deterministic per run, but two runs of the *same*
  plan, or two plans against the *same* profile, can still collide on a
  hypervisor's VMID / SDN zone / pool namespace.

These are not bugs to patch under the current model; they are the cost of a
feature (multi-instance concurrency) we have not built. Pretending otherwise in
the docs is the actual defect.

## Decision

**TestRange is single-instance. One `testrange` process runs at a time per
user and per driver profile.** The following are explicitly **unsupported** at
present, and not guarded beyond the ownership check below:

1. Two `testrange` processes running concurrently as the same user — even
   different plans, even different backends (they share the local cache and
   state root).
2. The same plan run twice concurrently.
3. Two different plans run concurrently against the same driver profile.

The atomic-rename writes and the `state.pid` ownership guard remain, reframed
for what they actually buy:

- **Atomic `.partial` + `os.replace` writes are crash safety, not
  serialization.** They guarantee that a SIGKILL / power loss mid-write leaves
  the canonical file fully-old or fully-new, never torn — for the *single*
  owning process. They do not order two writers, because by contract there is
  never more than one.
- **The ownership guard exists for the `cleanup` recovery tool, not for live
  concurrency.** `testrange cleanup` is run *after* a crashed/killed run to
  reverse its `state.json` ledger; the guard refuses to act on a run whose
  owner is still alive (that run's own `__exit__` owns its teardown). This is a
  dead-owner-vs-recovery-tool interaction, not two peers racing a live run. The
  mechanism is hardened to an advisory `fcntl.flock` under CORE-30 to close a
  PID-reuse window.

Running multiple ranges today means running them **serially**, or as separate
users with separate `XDG_STATE_HOME` / `XDG_CACHE_HOME` roots and distinct
profiles.

## Consequences

- The state/cache docstrings and PLAN.md §16 now say "single-instance crash
  safety", not "thread-safe" / "concurrent". The cross-process `FileLock` that
  PLAN.md once declined stays declined — there is no legitimate concurrent
  writer to serialize under this contract.
- Multi-instance support becomes a tracked, deliberate epic rather than an
  implied-but-broken capability. Three scenarios are filed, in increasing
  difficulty:
  - **ORCH-10** — one user, multiple *different* plans at once (shared cache:
    per-write `mkstemp`, content-verified HTTP landing, serialized state-root
    access).
  - **ORCH-11** — the same plan run twice (run-scoped backend naming verified
    collision-free; VMID / switch / SDN-zone collision handling).
  - **ORCH-12** — different plans against the same profile (connection sharing,
    per-run resource namespacing, zone/pool collision avoidance).
  These are distinct from **ORCH-1** (multiple `Hypervisor` entries in *one*
  plan) and **ORCH-4** (parallel build *within* a single run) — both of which
  are intra-process parallelism under a single owner.
- Defects that only manifest under multi-instance use (the fixed-name `.partial`
  races, B1/B2 in the review) are scoped to those tickets, not the
  single-instance hardening pass. Content-integrity fixes that matter even
  single-instance (the HTTP fetch re-hash, CACHE-3) are not deferred.
