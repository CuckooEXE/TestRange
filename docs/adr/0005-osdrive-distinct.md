# ADR-0005: ``OSDrive`` is a distinct class from ``HardDrive``

Status: Accepted
Date: 2026-05-11

## Context

PLAN.md decision 8. .bak's convention was "the first ``HardDrive`` in
``devices=[...]`` is the OS disk" — a silent footgun if the list is
ever reordered.

## Decision

- ``OSDrive(pool, size_gb)`` is a distinct dataclass.
- ``HardDrive(pool, size_gb)`` is a data disk.
- ``VMSpec.__post_init__`` enforces exactly one ``OSDrive`` per spec;
  zero or more ``HardDrive`` are allowed.

## Consequences

- The OS-install target is self-documenting and impossible to
  misuse by reordering.
- Builders consume ``spec.os_drive`` directly without positional
  conventions.
