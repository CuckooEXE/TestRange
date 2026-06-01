# ADR-0022: `xorriso` is sanctioned for PVE installer-ISO preparation

Status: Accepted
Date: 2026-05-31

**Invokes [ADR-0001](0001-subprocess-ban.md)'s escape hatch.** That ADR bans
`import subprocess` under `testrange/` but provides for exactly this case:
"If a future feature requires a subprocess directly from Python … it gets its
own ADR and a single sanctioned module at that time." This is that ADR, and
that module.

## Context

`ProxmoxAnswerBuilder` (BUILD-2) builds a Proxmox VE node as a guest via the
PVE 9.x auto-installer. Activating unattended mode requires injecting a small
file — `/auto-installer-mode.toml` (`mode = "partition"`,
`partition_label = "PROXMOX-AIS"`) — plus a `/proxmox-first-boot` script at the
**root of the installer ISO**. The installer's `proxmox-fetch-answer` entry
point reads that file off the booted media; absent it, the installer drops to
the interactive TUI.

The PVE installer ISO is a **hybrid image**: its UEFI boot path depends on a
precise El Torito + GPT/MBR/HFS+ layout. PVE's UEFI GRUB has an embedded
prefix-finding config that walks the GPT to locate the EFI System Partition;
without that GPT entry GRUB drops to its interactive `grub>` shell instead of
loading `/boot/grub/grub.cfg`.

A pure-`pycdlib` approach — `PyCdlib.open()` → `add_fp()` → `write_fp()`, the
same library ADR-0001 names for cloud-init seed authoring — **does not work
here**. `write_fp()` regenerates the image and preserves only the basic El
Torito boot record; it strips the hybrid GPT/MBR/HFS+ infrastructure. The
symptom is deterministic and was reproduced in the prior implementation: the
rebuilt ISO boots to `grub>`, never to the installer. This is a structural
limitation, not a tuning problem — `pycdlib` has no API to preserve an
appended GPT and relocate its backup header across an image that grew by a
file.

This is the cross-format-media case ADR-0001 anticipated when it carved out a
future-subprocess exception (it named "cross-format disk conversion when
ESXi/Hyper-V land" as the archetype). It is also consistent with the project's
existing scoped carve-outs: SFTP for PVE pool egress (no REST byte-channel),
and libvirtd's internal `qemu-img convert` behind a single stream-API call.

## Decision

**`xorriso` (libisoburn's CLI) is a sanctioned subprocess, used in exactly one
module: `testrange/builders/_proxmox_prepare.py`.** Nothing else under
`testrange/` may `import subprocess`.

The module drives:

```
xorriso -return_with FAILURE 32 \
        -indev  <vanilla.iso> \
        -outdev <prepared.iso> \
        -boot_image any keep \
        -map <auto-installer-mode.toml> /auto-installer-mode.toml \
        -map <first-boot.sh>           /proxmox-first-boot \
        -commit
```

- `-boot_image any keep` preserves the original El Torito catalog, the
  MBR/GPT hybrid layout, and the EFI System Partition pointer byte-for-byte
  while the new files are appended — which is the whole point.
- `-return_with FAILURE 32` lifts xorriso's exit threshold above the benign
  `SORRY` it emits when the original protective MBR encoded the original image
  size and the grown image is now slightly smaller than that entry implies.
  That metadata note is not a write failure (the "Writing … completed
  successfully" line precedes it) and the resulting ISO is bootable on all
  three paths (UEFI via GPT, BIOS via El Torito, hybrid-USB via MBR boot code).
  Real write-side problems still surface as `FAILURE`/`FATAL` and exit non-zero.

Enforcement carve-out (mirrors ADR-0001's mechanism):

- ruff's `flake8-tidy-imports.banned-api` gets a per-file ignore for
  `testrange/builders/_proxmox_prepare.py`.
- `tests/unit/test_subprocess_ban.py`'s source grep adds that one module to its
  whitelist.
- The module fails loud when `xorriso` is absent on `$PATH`, with the install
  hint (`apt install xorriso` / `dnf install xorriso` / `brew install
  xorriso`). `xorriso` ships with libisoburn on every mainstream distro and is
  already in the Proxmox toolchain.

PVE 9.x only. 8.x put the activation file inside the installer initrd; that is
a different prep strategy and out of scope.

## Consequences

- `testrange` gains exactly one auditable subprocess call site. The ban's value
  — "a sprawling subprocess surface area is the single hardest thing to audit
  later" — is preserved, because the surface is one module with one external
  command and a fixed argument vector built from internal data (no shell, no
  user-interpolated flags).
- `xorriso` becomes a runtime system dependency of the `proxmox` extra's
  installer-origin path. It is *not* required for the cloud-init builder, the
  libvirt reference backend, or any run-phase operation — only for *building* a
  PVE-node disk from installer media. Documented in the install notes for that
  extra.
- The prepared ISO is a derived, cacheable artifact keyed by
  `(vanilla sha + first-boot digest)`, so the subprocess runs once per
  `(ISO version, first-boot script)` pair, not per build.

## Alternatives considered

- **Pure-Python hybrid-ISO rewrite.** Preserve the primary system area, then
  relocate the backup GPT, recompute its CRC32s, and fix the protective-MBR
  partition-size entry every time the image grows by a file. Feasible in
  principle but it is bespoke GPT/El-Torito surgery, fragile across PVE ISO
  revisions, and `pycdlib` offers no hook for it today. Multi-day R&D to
  re-derive what `-boot_image any keep` does in one flag. Logged as a future
  hardening ticket if zero system deps ever becomes a hard requirement; not
  worth blocking BUILD-2 on.
- **`proxmox-auto-install-assistant prepare-iso`.** The vendor tool does
  exactly this, but it is a Proxmox-only binary not present on a generic
  orchestrator host, and shelling out to it is strictly worse than `xorriso`
  (same subprocess cost, narrower availability, no added safety).
- **Kernel-cmdline / HTTP fetch-from instead of a prepared ISO.** PVE 9.x reads
  the activation file off the booted media root; there is no clean cmdline
  alternative, and HTTP fetch-from would add a build-time web server to the
  orchestrator. The partition-label seed ISO we already author covers
  `answer.toml` delivery; only the activation file forces touching the boot
  ISO.
