# Contributing

## Dev setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

For libvirt integration tests, also install:

```sh
pip install -e '.[libvirt,ssh,cloudinit,http]'
```

## Standard gate (run before every commit)

```sh
ruff check .
ruff format --check .
mypy --strict testrange/
pytest -q
```

All four must pass. The ``libvirt`` mark gates integration tests; they
skip on machines without ``libvirt-python``.

## Discipline

- TDD: tests land before or alongside code.
- ``import subprocess`` is forbidden anywhere in ``testrange/`` (ruff
  enforces this; ADR-0001).
- ABCs live at the bottom of each subpackage; concretes import
  ABCs and never each other.
- The orchestrator is the only thing allowed to know about multiple
  stovepipes and broker between them.
- Resources go into ``state.json`` BEFORE the driver create-call.
- Deterministic names: ``driver.compose_resource_name`` and
  ``driver.compose_mac`` are pure functions of the run + plan + VM.

## Layout

See ``PLAN.md`` for the file-layout spec; ``docs/Architecture-and-Design.md``
for component overview; ``docs/adr/`` for load-bearing decisions.
