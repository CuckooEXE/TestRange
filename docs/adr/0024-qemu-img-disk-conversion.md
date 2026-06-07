# ADR-0024: `qemu-img` is sanctioned for disk-format conversion at a driver boundary

Status: Accepted
Date: 2026-06-01

**Invokes [ADR-0001](0001-subprocess-ban.md)'s escape hatch.** ADR-0001 bans
`import subprocess` under `testrange/` and named *exactly this case* as the
archetype it anticipated: "cross-format disk conversion when ESXi/Hyper-V land."
This is that ADR, and `testrange/drivers/_diskconvert.py` is that module.

## Context

The cache is **qcow2 cache-wide** (decision A, 2026-06-01): a built/base disk is
content-addressed by the sha256 of its qcow2 bytes, and every disk the
orchestrator threads between driver calls is qcow2. The libvirt and Proxmox
backends are qcow2-native, so nothing converts.

The ESXi backend is **not** qcow2-native — VMFS disks are `vmdk`. The driver must
therefore convert at its boundary, both directions:

- **ingest** (`upload_to_pool`): qcow2 → vmdk, landed on the datastore, then
  inflated to a bootable + growable VMFS disk (the inflate is ESXI-3 / ESXI-S2,
  not this module);
- **egress** (`download_from_pool`): a single self-contained vmdk the backend
  exported → qcow2, read back into the cache.

`qemu-img` is the obvious tool, but it is a **host binary**, not a Python wheel —
there is no `_import_<dep>()` to call, so it cannot ride the lazy-import pattern
the SDK dependencies use. It must be invoked as a subprocess.

## Decision

**`qemu-img` is a sanctioned subprocess, used in exactly one module:
`testrange/drivers/_diskconvert.py`.** It is backend-agnostic (it generalizes to
Hyper-V `vhdx` later) and lives under `drivers/`, not in any one driver package.

- **Host-binary dependency, not a wheel.** Discovered via `shutil.which`.
  `require_qemu_img()` fails loud with an install hint
  (`apt install qemu-utils` / `dnf install qemu-img` / `brew install qemu`).
  Preflight calls it (ESXI-9) so an image-origin build on a non-qcow2 backend
  fails on the orchestrator host *before* any backend resource stands up, not
  mid-upload.
- **Fixed, internal-data-only argument vector.** `qemu-img convert [-f IN] -O
  OUT [-o subformat=SUB] SRC DST` — no shell, no user-interpolated flags. `-f`
  pins the source format where known (a probe is a small attack surface on
  untrusted images).
- **Direction helpers.** `qcow2_to_vmdk(subformat="streamOptimized")` and
  `vmdk_to_qcow2`. `streamOptimized` is the single-file, self-contained
  *transport* subformat — it is **not** runnable; a VM cannot boot off it until
  the ESXi driver inflates it to a VMFS disk (ESXI-S2 owns the inflate). The
  subformat is selectable because the inflate path constrains which transport
  vmdk it accepts.
- **The on-datastore vmdk is derived and ephemeral — never content-addressed.**
  `qemu-img` vmdk output is not byte-deterministic, so only the qcow2 is keyed
  by content; the vmdk is a throwaway projection regenerated on demand.

Enforcement carve-out (mirrors ADR-0022 / ADR-0001's mechanism):

- ruff's `flake8-tidy-imports.banned-api` gets a per-file ignore (TID251/S404/
  S603) for `testrange/drivers/_diskconvert.py` in `pyproject.toml`.
- `tests/unit/test_subprocess_ban.py`'s source-grep whitelist adds exactly this
  module and asserts it still imports `subprocess`, so a stale entry can't
  silently widen the ban.

## Consequences

- `testrange` gains one more auditable subprocess call site — one module, one
  external command, a fixed argument vector. The ban's audit value is preserved.
- `qemu-img` becomes a runtime host dependency of the **image-origin build path
  on a non-qcow2 backend** (ESXi today). It is *not* required for the libvirt or
  Proxmox backends, nor for an installer-origin ESXi build (which lands a blank
  VMFS disk the installer partitions — no qcow2 source to convert). Documented
  in the ESXi driver setup page (ESXI-14).
- Conversion is a host-CPU/host-disk cost on the orchestrator, paid once per
  disk crossing the boundary (ingest of a cache miss, egress of a built disk).

## Alternatives considered

- **Convert inside the backend (vmkfstools over SSH).** Rejected: ESXi byte
  I/O is the datastore `/folder` HTTPS endpoint and the pyVmomi control plane,
  per the ESXi ADR — no host shell on the hypervisor, and vmkfstools is a
  second, ESXi-only subprocess surface. `qemu-img` on the orchestrator keeps the
  conversion host-side and backend-agnostic.
- **A pure-Python vmdk writer.** A qcow2→streamOptimized-vmdk encoder is
  bespoke, format-version-fragile, and re-derives what `qemu-img` does in one
  call. Not worth blocking the ESXi epic on; logged as a future zero-host-deps
  hardening option, same as ADR-0022's pure-Python ISO rewrite.
