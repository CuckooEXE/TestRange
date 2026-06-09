# Contributing

## Working agreement (hard rules)

These are non-negotiable for this repo (the canonical copy lives in the
repo-root `CLAUDE.md`):

- **Ticket-first.** Every change needs a task on the board in
  [`TODO.md`](../../TODO.md) before work starts — it is both the live status and
  the audit trail, and versions with the code. No off-the-books fixes.
- **Gates always pass.** The standard gate below must be green on every commit —
  no `# type: ignore` to dodge mypy, no skipped tests, no `--no-verify`. If a
  gate fails, the work isn't done.
- **New driver-facing capabilities land in the certification corpus.** Anything
  that touches a driver-facing contract (devices, networks, communicators,
  builders, snapshots, power state, pools, …) must be covered in
  [`tests/plans/`](../../tests/plans/README.md) **in the same change** — extend
  the relevant plan (or add one) plus a `TESTS` entry. Portable capabilities go
  in `tests/plans/generic/` (must run on every backend); backend-specific ones
  go in `tests/plans/<driver>/`.
- **`PLAN.md` and `TODO.md` are sources of truth.** `PLAN.md` is the living
  design; a change that alters design or scope is incomplete until both are
  updated to match. If code and `PLAN.md` disagree, that's a bug to fix.

## Dev setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For backend integration tests, install the full set of extras:

```sh
pip install -e '.[all,dev]'
```

## Standard gate (run before every commit)

```sh
ruff check .
ruff format --check .
mypy --strict testrange tests
pytest -m "not proxmox and not libvirt"
```

All four must pass (this mirrors `.pre-commit-config.yaml`; note mypy covers
both ``testrange`` and ``tests``). The ``proxmox`` and ``libvirt`` marks gate
integration tests against a live backend (they run out-of-band, skipping without
the backend configured); the unit suite runs entirely against the in-memory
``MockDriver`` and needs no backend.

## Discipline

- TDD: tests land before or alongside code.
- ``import subprocess`` is forbidden anywhere in ``testrange/`` (ruff
  enforces this; ADR-0001), except the ADR-0022-sanctioned ISO-prep modules
  (``builders/_proxmox_prepare.py``, ``builders/_esxi_prepare.py``) that shell
  out to ``xorriso``.
- ABCs live at the bottom of each subpackage; concretes import
  ABCs and never each other.
- The orchestrator is the only thing allowed to know about multiple
  stovepipes and broker between them.
- Resources go into ``state.json`` BEFORE the driver create-call.
- Deterministic names: ``driver.compose_resource_name(run_id, kind, name)``
  is a pure function of run + kind + name; ``driver.compose_mac(plan_name,
  vm_name, nic_idx)`` is a pure function of plan + VM + NIC index
  (deliberately run-independent, so a VM's MAC is stable across runs — see
  ADR 0006).

## Where to go next

- [Architecture](architecture.md) — component overview.
- [Extending](extending/index.md) — how to add a new driver, device,
  communicator, or builder.
- [Bugfixing](bugfixing.md) — reproduction, diagnosis, regression tests.
- [ADRs](../adr/index.md) — load-bearing decisions.

## Building the docs

```sh
pip install -e '.[docs]'
make -C docs html
```

The HTML lands at `docs/_build/html/index.html`.
