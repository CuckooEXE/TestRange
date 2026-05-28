# Contributing

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
pytest -m "not proxmox"
```

All four must pass (this mirrors `.pre-commit-config.yaml`; note mypy covers
both ``testrange`` and ``tests``). The ``proxmox`` mark gates integration tests against a
live Proxmox VE host (they skip without ``TESTRANGE_PVE_HOST`` configured); the
unit suite runs entirely against the in-memory ``MockDriver`` and needs no
backend.

## Discipline

- TDD: tests land before or alongside code.
- ``import subprocess`` is forbidden anywhere in ``testrange/`` (ruff
  enforces this; ADR-0001).
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
