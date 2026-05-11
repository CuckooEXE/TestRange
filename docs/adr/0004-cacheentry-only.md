# ADR-0004: Base disks referenced via ``CacheEntry`` only

Status: Accepted
Date: 2026-05-11

## Context

PLAN.md decision 11: URLs and filepaths are dropped from Plan-time
entirely. The only way to use a base disk in a Plan is to first
``testrange cache add`` it, then reference it via
``CacheEntry("identifier")``.

This makes Plans fully portable — no developer-specific filepaths or
upstream URLs leak into them — and the cache becomes the single
source of truth for content identity.

## Decision

- ``LocalCache`` stores ``<sha>.bin`` + sidecar ``<sha>.json``
  metadata under ``$XDG_CACHE_HOME/testrange/isos/``. Atomic writes
  via ``.partial`` + ``os.replace``.
- The sidecar is self-describing: ``sha256``, ``size``, ``names[]``
  alias list, ``origin``, ``added_at``, ``description``.
- ``CacheEntry("...")`` auto-detects: matches ``^[0-9a-f]{16,64}$`` →
  content sha; otherwise pretty-name. Names are global and unique
  within the cache.
- An entry can have multiple aliases (``cache rename`` appends).
- ``testrange describe`` resolves entries best-effort (warns on
  miss). ``testrange run`` preflight fails loud on cache miss with a
  ``testrange cache add ...`` fix hint.

## Consequences

- The HTTP cache tier (when added) doesn't change Plan code; only the
  CLI ``--cache URL`` flag attaches it.
- Format detection (qcow2/raw/vmdk/...) is a driver concern, NOT a
  cache concern. The cache treats every entry as opaque bytes.
