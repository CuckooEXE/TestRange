# ADR-0001: No `subprocess` in v0

Status: Accepted
Date: 2026-05-11

## Context

Every operation v0 needs has a Python library: ``libvirt-python`` for
the hypervisor, ``paramiko`` for SSH, ``pycdlib`` for cloud-init seed
ISO authoring, stdlib ``urllib`` for HTTP fetches in the cache, and
``cryptography`` for keypair generation.

Per PLAN.md decision 15, v0 forbids ``import subprocess`` anywhere in
the package. The justification is the .bak experience: a sprawling
subprocess surface area is the single hardest thing to audit later.

## Decision

- ``import subprocess`` is rejected by ruff's
  ``flake8-tidy-imports.banned-api`` rule.
- A unit test (``tests/unit/test_subprocess_ban.py``) greps the source
  tree as a CI safety net for environments where ruff isn't run.
- If a future feature requires a subprocess (the leading candidate is
  ``qemu-img`` for cross-format disk conversion when ESXi/Hyper-V
  land), it gets its own ADR and a single sanctioned module at that
  time.

## Consequences

- ``LibvirtDriver`` creates qcow2 overlays via libvirt's
  ``<backingStore>`` volume XML (no ``qemu-img`` shell-out).
- The cloud-init seed is built with ``pycdlib`` (no
  ``genisoimage``).
- Cache downloads go through ``urllib.request.urlopen``, not
  ``curl`` / ``wget``.
- HTTP cache (when added) uses ``requests`` or stdlib ``urllib``.
