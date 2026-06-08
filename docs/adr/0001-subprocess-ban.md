# ADR-0001: No `subprocess` in `testrange/`

Status: Accepted
Date: 2026-05-11

## Context

Every operation `testrange` needs has a Python library: ``libvirt-python``
for the hypervisor, ``paramiko`` for SSH, ``pycdlib`` for cloud-init seed
ISO authoring, stdlib ``urllib`` for HTTP fetches in the cache, and
``cryptography`` for keypair generation.

`testrange` forbids ``import subprocess`` anywhere in the package. The
justification is prior experience: a sprawling subprocess surface area is
the single hardest thing to audit later.

## Decision

- ``import subprocess`` is rejected by ruff's
  ``flake8-tidy-imports.banned-api`` rule.
- A unit test (``tests/unit/test_subprocess_ban.py``) greps the source
  tree as a CI safety net for environments where ruff isn't run.
- If a future feature requires a subprocess directly from Python (cross-
  format disk conversion when ESXi/Hyper-V land, for example), it gets
  its own ADR and a single sanctioned module at that time.

## Caveat: libvirt-internal subprocesses

The ban applies to code under ``testrange/``. libvirtd itself invokes
external tools — ``qemu``, ``qemu-img``, ``dnsmasq`` — and that's
libvirtd's business. In particular, ``LibvirtDriver.download_from_pool``
flattens volumes via ``pool.createXMLFrom``, which under the dir-pool
driver invokes ``qemu-img convert`` inside the libvirtd process. That is
not a violation: ``testrange`` made one libvirt API call, and libvirt
chose to satisfy it via a subprocess on its own side.

## Consequences

- ``LibvirtDriver`` creates copy-on-write overlays via libvirt's
  ``<backingStore>`` volume XML (no ``qemu-img`` shell-out from us).
- Pool I/O goes through libvirt's stream API, not direct file reads.
- The cloud-init seed is built with ``pycdlib`` (no ``genisoimage``).
- Cache downloads go through ``urllib.request.urlopen``, not
  ``curl`` / ``wget``.

> **Addendum (2026-06-08, DOCS-20):** the libvirt-internals examples above
> describe the *pre-rebuild* driver and are now historical. The rebuilt
> reference driver (ADR-0019) uses **full-content qcow2 with no backing chains**
> — there is no ``<backingStore>`` overlay and no ``createXMLFrom``/``qemu-img
> convert`` flattening in ``download_from_pool``; volume bytes stream through the
> libvirt stream API. The subprocess-ban decision itself is unchanged; only the
> illustrative libvirt mechanics drifted. (qcow2-cache-wide conversion at a
> non-qcow2 boundary is the sanctioned exception in ADR-0024.)
