"""Bounded intra-process parallelism for the orchestration I/O phases.

A single ``testrange run`` is otherwise sync and single-threaded (ADR-0002).
The I/O phases — bring-up uploads, build-disk downloads, readiness waits — are
almost entirely blocking C/socket I/O, so a bounded thread pool overlaps them
for real wall-clock wins while the public API stays synchronous and the
single-instance contract (ADR-0018) is untouched. Test execution stays
sequential.

The design rests on one rule (see the concurrency ADR): a single, shared driver
connection is driven concurrently. libvirt's ``virConnect`` is internally
thread-safe and serves concurrent streams, and proxmoxer's session / paramiko
transport multiplex concurrent requests/channels — so the slow transfers
overlap on one connection. The drivers' in-memory resource maps stay consistent
because there is one driver instance, and agent commands serialize on the
driver's own ``call_lock``. Shared bookkeeping (``StateStore`` RMW, the
``RunContext`` ledger) is mutated only under its own lock, held briefly.

Per-worker connections were considered and rejected: the drivers cache
cross-call resource-name resolution in per-instance maps (libvirt
``_libvirt_net_by_network``, proxmox ``_vnet_by_network`` + a random
``_sdn_zone``), which a second connection cannot see — a network created on one
connection is unresolvable on another.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from typing import TypeVar

from testrange._log import get_logger

_log = get_logger(__name__)

_T = TypeVar("_T")
_R = TypeVar("_R")

# Default ceiling on concurrent workers per phase when the caller passes no
# explicit ``jobs``. Bounded so a large plan cannot open an unbounded number of
# backend connections at once; the ``--jobs`` CLI flag overrides it.
DEFAULT_MAX_WORKERS = 8


def resolve_workers(n_items: int, jobs: int | None) -> int:
    """How many workers to run for ``n_items`` of work given an optional cap.

    Never exceeds the item count (no idle threads), never below 1, and falls
    back to :data:`DEFAULT_MAX_WORKERS` when ``jobs`` is ``None``.
    """
    if n_items <= 1:
        return 1
    cap = DEFAULT_MAX_WORKERS if jobs is None else jobs
    return min(max(cap, 1), n_items)


def parallel_map(
    fn: Callable[[_T], _R],
    items: Iterable[_T],
    *,
    jobs: int | None = None,
) -> list[_R]:
    """Apply ``fn`` to every item, concurrently, returning results in input order.

    Bounded by :func:`resolve_workers`. A single item (or ``jobs<=1``) runs
    inline with no thread overhead, so the serial path is unchanged. The first
    worker to raise — taken in *submission* order for determinism — propagates
    its exception (with traceback) after the still-pending workers are
    cancelled; in-flight workers are joined by the executor on exit. This is the
    one place the worker cap and the fail-fast error policy live.
    """
    work = list(items)
    if not work:
        return []
    workers = resolve_workers(len(work), jobs)
    if workers == 1:
        return [fn(item) for item in work]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tr-worker") as ex:
        futures: list[Future[_R]] = [ex.submit(fn, item) for item in work]
        _done, not_done = wait(futures, return_when=FIRST_EXCEPTION)
        for fut in futures:  # submission order → deterministic first failure
            if fut.done() and fut.exception() is not None:
                for pending in not_done:
                    pending.cancel()
                fut.result()  # re-raise the worker's exception with its traceback
        return [fut.result() for fut in futures]


__all__ = ["DEFAULT_MAX_WORKERS", "parallel_map", "resolve_workers"]
