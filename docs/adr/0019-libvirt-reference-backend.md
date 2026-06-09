# ADR-0019: libvirt is the reference backend; the mock is test-only

Status: Accepted
Date: 2026-05-31

**Closes [ADR-0008](0008-driver-abc-multi-backend.md)'s interim.** That ADR
deleted the original libvirt driver and stood up `MockDriver` as the test
substrate "in the interim … rebuilt later against this ABC." The rebuild
(BACKEND-1) is now certified, so this ADR settles which backend is the
*reference* implementation and what role the mock keeps.

## Context

When the driver ABC was reshaped for four backends (ADR-0008), the only
working driver was an in-memory `MockDriver`. By default it became the thing
everything pointed at: `examples/`-adjacent docs called it "the reference
implementation," it lived in the shipped package at `testrange/drivers/mock.py`,
and it auto-registered via a side-effect import in `drivers/__init__.py` — so a
plain `pip install testrange` shipped a fake backend as a first-class, listed
driver.

That was right while it was the *only* end-to-end-exercised driver. It is wrong
now. The libvirt driver has been rebuilt from zero against the current ABC
(BACKEND-1.0..1.E) and **certified**: as a plain `libvirt`-group user, no root,
against a local `qemu:///system`,

- `testrange run --profile libvirt-local examples/capabilities.py` is green
  across the full portable survey, and
- `pytest -m libvirt` (`tests/integration/test_libvirt.py`) is green.

A real backend that drives VM lifecycle, L2 via the libvirt network API, the
serial build-result sink, QGA guest-ops, per-run directory pools, and streamed
volume I/O is a far better reference for "what a driver must do" than an
in-memory model that simulates the backend rather than a real guest. Keeping the
mock labeled "reference" understates libvirt and overstates the mock — the mock
can't surface qcow2 chain semantics, permission boundaries, or stream-API
quirks, which is exactly the class of contract a *reference* is supposed to pin.

## Decision

**The libvirt driver (`testrange/drivers/libvirt/`) is the reference backend
implementation.** It is the canonical answer to "how does a driver satisfy the
ABC end-to-end," and the certification gate — `examples/capabilities.py` green
plus `pytest -m libvirt` green, both as a plain user — is the bar every future
backend (ESXi, Hyper-V) is held to.

**`MockDriver` is a test-only fixture, not a shipped backend.** Concretely
(landed under BACKEND-1.E):

- It lives at `tests/mock_driver.py`, **not** in the `testrange` package.
- Its side-effect registration was dropped from `drivers/__init__.py`; the mock
  scheme is registered in `tests/conftest.py`, so unit tests and
  `--profile mock` resolve it in-process but a production install does not list
  it.
- It remains the substrate the orchestrator/ABC unit suite drives end-to-end
  (`tests/unit/`), because the full lifecycle on the mock needs no live
  hypervisor and runs in CI without a backend. That role is unchanged — only its
  *status* (test fixture, not reference) and *location* (tests/, not package)
  change.

Docs follow suit: the extending-a-driver guide points at the libvirt driver as
the worked example and names the mock as the in-memory test substrate; the user
docs describe libvirt as the certified reference backend (already landed under
DOCS-5).

## Consequences

- "Reference implementation" now means a real, certified backend, so new
  drivers have a truthful model to read: a driver that actually talks to a
  hypervisor, not one that fakes the responses. The deviation analysis in
  ADR-0008 plus the libvirt source are the two things to read.
- A production `pip install` no longer ships a fake backend in its driver
  registry. `mock` is reachable only from the test tree (via conftest), which
  matches its purpose.
- The certification gate is now a stable, named bar. ESXi (BACKEND-2) and
  Hyper-V (BACKEND-3) are "done" when they pass the same two conditions the
  libvirt driver passed; until then they are roadmap, not reference.
- The mock's value as a *fast, hermetic* substrate is preserved — the unit
  suite still exercises the full build/run lifecycle against it with faked
  communicators, with no live hypervisor required.

> **Addendum (2026-06-08, DOCS-16):** the certification gate's
> `examples/capabilities.py` clause was superseded by **ADR-0028** (REL-2): the
> portable survey became the `tests/plans/` corpus, run via `testrange run
> --profile libvirt-local tests/plans/<tier>/<plan>.py` (generic + `libvirt/`
> sweeps), and `examples/capabilities.py` was retired. The gate is otherwise
> unchanged — that corpus green **plus** `pytest -m libvirt` green, both as a
> plain `libvirt`-group user — and remains the bar every backend is held to.
