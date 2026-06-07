# ADR-0026: Libvirt-concrete device types for disk bus / NIC model

Status: Accepted
Date: 2026-06-02

## Context

Portable device types (`testrange.devices`) describe hardware in
backend-agnostic terms: `OSDrive(pool, size_gb)`, `HardDrive(...)`,
`NetworkIface(network, addr)`. The libvirt driver has always emitted a fixed
device model for them — **virtio-blk** disks (`vda`, `vdb`, …) and **virtio-net**
NICs — which is the right default for Linux guests (fast paravirtual drivers,
inbox everywhere).

Nesting an **ESXi** guest on the libvirt L0 (ADR-0021 amendment, ORCH-32) breaks
that assumption: ESXi ships **no virtio drivers at all**. Its OS disk must hang
off a `sata`/`ide`/`scsi` controller and its NIC must be an emulated `e1000e` —
otherwise the installer sees no disk and no network. So the libvirt driver needs
a way for a plan to *select* the disk bus and NIC model, while every existing
plan keeps the virtio default unchanged.

Two backends already faced the per-device-knob question and answered it the same
way: `ProxmoxHardDrive(HardDrive, bus=...)` and `ESXiHardDrive(HardDrive,
bus=...)` are backend-specific subclasses of the portable types. The portable
`VMSpec` accessors collect devices by `isinstance`, so a subclass flows through
unchanged; the driver reads the extra field when it recognizes its own subtype.
We reuse that established pattern rather than inventing a portable `bus=`/`model=`
knob on the generic types (which would be a backend concept leaking into the
portable surface, and dead weight for every backend that doesn't honor it).

## Decision

Add libvirt-concrete device variants, as subclasses of the portable types:

- `testrange/devices/disk/libvirt.py` — a `_LibvirtDisk(_Disk)` base adds
  `bus: str = "virtio"` (validated against `{virtio, sata, ide, scsi}`), and
  `LibvirtOSDrive(_LibvirtDisk, OSDrive)` / `LibvirtDataDrive(_LibvirtDisk,
  HardDrive)` are the concretes. The MRO resolves dataclass fields to
  `pool, size_gb, bus` (the default-valued `bus` last), so construction is
  ordinary; `__post_init__` chains `super().__post_init__()` so the base
  `_Disk` invariants (non-empty pool, positive size) still bite.
- `testrange/devices/network/libvirt.py` — `LibvirtNetworkIface(NetworkIface)`
  adds `model: str = "virtio"` (validated against `{virtio, e1000, e1000e,
  rtl8139}`).

These live in the **devices** package under driver-named submodules (matching
the `testrange/devices/__init__.py` note "any driver-specific variant lives
under its driver-named submodule and is imported directly from there"), not
re-exported from the generic `testrange.devices`. (The pre-existing
`Proxmox*`/`ESXi*` variants instead live under `testrange/drivers/<backend>/`;
that minor placement inconsistency is left as-is — not worth a churn pass.)

The libvirt driver (`drivers/libvirt/_vm.py`) reads them:

- `_disk_xml`/`_interface_xml` take `bus`/`model` parameters (default virtio).
- Device-node names are allocated by a **shared per-bus-prefix counter** (`vd*`
  for virtio, `sd*` for sata/scsi, `hd*` for ide), across disks *and* CDROMs, so
  a non-virtio OS disk (e.g. a sata ESXi guest) and the sata seed/installer
  CDROMs never land on the same letter. The all-virtio case still yields
  `vda, vdb, …, sda, sdb` — byte-identical to the prior hard-coded layout.
- A build-phase **build NIC** inherits the guest's first declared
  `LibvirtNetworkIface` model (`_build_nic_model`), so an ESXi-shaped guest
  installs over an `e1000e` it can actually drive (the declared NICs are
  replaced by the single build NIC during install).

## Consequences

- Existing plans are unchanged: a plain `OSDrive`/`NetworkIface` still emits
  virtio-blk/virtio-net, same `vda`/`vdb` ordering the `fileserver` capability
  depends on.
- A plan that uses a `Libvirt*` device is, by construction, pinned to the libvirt
  backend — the portability lint (`compatibility_findings`) is the hook that
  would reject binding it elsewhere, exactly as for the `Proxmox*` variants.
- The knob is intentionally narrow (one bus enum, one model enum) — only what the
  nested-ESXi case needs. NVMe, virtio-scsi controllers, multiqueue, etc. are not
  modeled until a plan needs them (no speculative surface).
- This is the enabling piece for the ESXi inner backend (ADR-0021 amendment): the
  nested ESXi guest declares `LibvirtOSDrive(bus="sata")` + `LibvirtNetworkIface(
  model="e1000e")`.
