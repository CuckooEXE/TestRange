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

## Caveat (added post-acceptance)

A later dependency, ``pyroute2`` (added with the libvirt bridge
management), uses ``asyncio`` *internally*. As with libvirtd's
subprocesses in ADR-0001, this does not violate the decision: ``testrange``
code drives pyroute2 through its synchronous ``IPRoute()`` API and never
touches an event loop. The "no asyncio in ``testrange/``" rule still holds;
the Context's blanket claim that no dependency has an asyncio variant is
simply no longer literally true.

## Consequences

- The public API is fully synchronous. No ``async def`` anywhere in
  ``testrange/``.
- A future parallel install pass is a long-term TODO; it'll need a
  per-driver ``RLock`` (libvirt-python isn't fully thread-safe) plus
  whatever cross-process locking the cache needs.

> **Addendum (2026-06-08, DOCS-20):** two things above have since changed.
> (1) The "no ``ThreadPoolExecutor``" line and the "future parallel pass" TODO
> were superseded by **ADR-0023** (in-process I/O parallelism): I/O phases now
> run on a bounded ``ThreadPoolExecutor`` (``--jobs``) behind a per-driver
> ``call_lock``, while *tests* still run serially. The "no ``asyncio`` in
> ``testrange/``" decision is unchanged. (2) The pyroute2 caveat is moot —
> pyroute2 was **dropped** (ADR-0016); bridges are built by the libvirt daemon
> via the network API, so no dependency in the tree carries an asyncio variant
> anymore.
