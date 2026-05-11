# ADR-0002: No asyncio in v0; sync, single-threaded

Status: Accepted
Date: 2026-05-11

## Context

Every dependency v0 uses is synchronous and blocking:
``libvirt-python``, ``paramiko``, ``pycdlib``, ``cryptography``,
``urllib``. None of them have asyncio variants. Mixing asyncio with
blocking code adds complexity for no functional gain at v0 scale.

## Decision

v0 runs single-threaded:

- Install brings up one VM at a time.
- Tests run sequentially in declaration order.
- No ``asyncio``, no ``ThreadPoolExecutor``.

State-file safety:

- Each write is atomic (``.partial`` + ``os.replace``).
- A sibling ``state.pid`` file records the owning PID; ``testrange
  cleanup`` refuses to act if that PID is still alive.

Replaced PLAN.md's earlier ``filelock.FileLock`` approach with the PID
file — simpler and produces a meaningful error message.

## Consequences

- The public API is fully synchronous. No ``async def`` anywhere.
- A future parallel install pass is a long-term TODO; it'll need a
  per-driver ``RLock`` (libvirt-python isn't fully thread-safe) plus
  whatever cross-process locking the cache needs.
