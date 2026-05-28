# Architecture and Design

`testrange` is a declarative test-range orchestrator: a user writes a
`Plan` (a Python dataclass tree), hands it to the orchestrator, and
the orchestrator brings up VMs against a hypervisor, runs user test
functions, and tears the range down.

## High-level shape

```
Plan(name, MockHypervisor(networks=, pools=, vms=[VMRecipe(...)]))
                                            │
                                            ▼
                          Orchestrator
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
       CacheManager   HypervisorDriver  StateStore
       (local-only,   (registry; MockDriver  (state.json
        for now)       in-memory today;       + state.pid)
                       libvirt/Proxmox/ESXi/
                       Hyper-V planned)
```

## Key components

- **`Plan(name, *hypervisors)`** — the top-level user declaration.
  Currently exactly one hypervisor; the variadic shape is locked in for
  future multi-hypervisor without changing the call shape.
- **`MockHypervisor(networks=, pools=, vms=, build_switch=, ...)`** —
  the top-level entry selecting the in-memory `MockDriver` (the reference
  backend until the real ones are written). Each backend ships its own such
  dataclass; the driver is constructed from its class via the driver registry
  (`testrange.drivers.driver_for`). `build_switch` is the user-declared build
  network (`Switch | ManagedBuildSwitch | None`, ADR-0014); `None` means an
  isolated build network with no egress (see the build phase below).
- **`VMSpec`** — hardware-only (`name`, `devices=[CPU, Memory,
  OSDrive, HardDrive, NetworkIface]`). Singleton-device runtime
  checks enforce exactly one CPU/Memory/OSDrive per spec.
- **`VMRecipe(spec=, builder=, communicator=)`** — provisioning
  declaration. `builder` (e.g., `CloudInitBuilder`) holds the
  credentials and bakes them into the disk; `communicator` (e.g.,
  `SSHCommunicator("user")`) names how the runtime talks to the
  brought-up VM.
- **`CacheEntry("identifier")`** — content-addressed reference into
  the local cache. ISOs and base disks are NEVER referenced by URL
  or filepath inside a Plan.
- **`CacheManager` / `LocalCache`** — `$XDG_CACHE_HOME/testrange/isos/`
  with `<sha>.bin` + sidecar `<sha>.json`. Atomic writes via
  `.partial` + `os.replace`.
- **`HypervisorDriver`** ABC — connect, preflight, switch/network/pool/VM
  CRUD, stable MAC derivation, an optional native-guest transport
  (`native_guest_execute` / `native_guest_read_file` /
  `native_guest_write_file`), and volume transport
  (see Pool I/O below). **The driver owns L2**: `create_switch` /
  `destroy_switch` realize the whole fabric (host bridge, vSwitch, vmbr+SDN,
  VMSwitch) and `create_network` attaches port-groups to it — the orchestrator
  never names a bridge (see [ADR-0008](../adr/0008-driver-abc-multi-backend.md)).
  DHCP lease lookup is deliberately *not* a driver method: the per-Switch
  sidecar owns DHCP, so a lease lives in the sidecar's `dnsmasq` lease file,
  which the orchestrator reads over the native-guest transport — not in
  anything the hypervisor manages. Concretes register themselves with the
  driver registry at import time. Today: `MockDriver` (in-memory).
- **Per-Switch sidecar VM** — a pre-built Alpine image with
  `dnsmasq`, `nftables`, and `qemu-guest-agent` baked in
  (`tools/build-sidecar-image/build.sh`). The orchestrator
  materializes one per Switch with `needs_sidecar` (= `switch.sidecar is
  not None`). Per-run config is delivered as a tiny ISO9660
  (`TR_SIDECAR_CFG`) carrying `dnsmasq.conf`, `interfaces`,
  `nftables.nft`, and `sysctl.conf` rendered by
  `testrange/networks/sidecar.py`. The sidecar IS the gateway when its
  `Sidecar` has `nat`; no hypervisor-native NAT/DHCP/DNS is used anywhere
  (build or run phase).
- **Pool I/O** — `upload_to_pool` (host file → in-pool volume) and
  `download_from_pool` (in-pool volume → host file) move bytes between the
  runner host and the backend; the orchestrator never opens pool files
  directly. Caching lives on the runner, never on the backend, so every driver
  must provide *some* transfer channel here — SDK-native where possible (libvirt
  stream, ESXi `/folder` HTTPS), or an out-of-band side channel (Proxmox over
  SSH, Hyper-V over SMB/WinRM). **Every disk reaches the backend by host→pool
  upload** — there is no pool→pool copy and no shared base
  ([ADR-0010](../adr/0010-build-run-split.md) §3). `create_blank_volume(ref,
  size_gb)` provisions a blank sized data disk and `resize_volume(ref, size_gb)`
  grows the image-based OS disk before the build boot; both produce
  self-contained volumes, so `download_from_pool` just streams them. A "pool" is
  a named namespace inside pre-existing backing storage (a libvirt pool, a
  datastore subdirectory, a host dir/share), not storage the driver provisions.
- **`StateStore`** — `$XDG_STATE_HOME/testrange/runs/<run_id>/state.json`.
  Each resource is recorded with `intent_at` (before backend call)
  and `outcome_at` (after backend confirms). Metadata is stamped at
  intent time as well as merged at confirm time, so a crash between
  the two still leaves cleanup enough information to route correctly.
  Atomic-rename writes; PID-gated cleanup via the sibling `state.pid`
  file.
- **`Orchestrator`** — phase-sequencing context manager.
- **`SSHCommunicator`** — paramiko-backed transport with shlex-joined
  argv exec, SFTP read/write, retry loop on connect.

## Phases

`build` and `run` are independent phases ([ADR-0010](../adr/0010-build-run-split.md)).
`testrange build` runs preflight + build only (warms the cache, no tests);
`testrange run` auto-builds any cache miss, then brings the range up and runs
tests (`--require-cache` makes a miss fail fast instead of building). The
`Orchestrator` composes both for the test-runner path.

1. **Pre-Flight** — read-only checks (subnet overlap, cache
   resolvability, pool-root writable). Returns `PreflightReport`.
   Errors abort before any state.json write.
2. **Build** — warms the cache, nothing else. First it resolves each VM's base,
   computes `builder.config_hash(...)`, and probes the cache for the VM's full
   *disk set* (OS disk + every data disk, each named `_built_<hash>__{os,dataN}`);
   a VM is cached only if **all** its artifacts are present. **Only if at least
   one VM misses** does it stand up a single ephemeral build pool + a transient
   build Switch (resolved from the Hypervisor's `build_switch` via
   `resolve_build_switch`, ADR-0014) + the per-Switch sidecar,
   and loop over only the missing VMs. Each missing VM is provisioned as a unit:
   its base is `upload_to_pool`-ed straight onto its own OS-disk ref and
   `resize_volume`-d up; each `HardDrive` is a `create_blank_volume`; the seed is
   written; the VM boots with **all** disks attached and self-terminates via
   `runcmd: [..., poweroff]`. On power-off every writable disk is
   `download_from_pool`-ed and `cache.add`-ed (and pushed to the HTTP tier when
   configured). The build VM and its disks are destroyed immediately after
   capture; the build pool, switch, and sidecar are torn down at phase end. The
   backend holds no testrange state between phases.
3. **Run** — creates the user's `pools` (build leaves none behind), then for
   every user Switch: `provision_switch` calls `driver.create_switch` (the driver
   realizes the fabric — bridge / vSwitch / vmbr / VMSwitch — and, for
   `nat + uplink`, the uplink-facing segment for the sidecar's `eth1`), attaches
   each Network with `driver.create_network`, and `materialize_sidecar_for` stands
   up the per-Switch sidecar VM when `dhcp|dns|nat` is set. The phase then blocks
   until every sidecar is **ready** (its native guest agent answers and the
   delivered `dnsmasq.conf` reads back) so DHCP is being served before any guest
   boots. Then each user VM gets its cached built disks (OS + each data disk)
   `upload_to_pool`-ed onto its own refs — no clone — and is defined with all
   disks attached and no seed, then started.
4. **Test** — communicators are bound (discovered IP per VM), then
   each builder's `wait_ready` runs: the orchestrator hands the
   builder the bound communicator's `execute` callable (a `GuestExec`
   from `testrange.guest_io`), the builder runs its own readiness
   command and raises `BuildNotReadyError` if the VM never becomes
   ready — before any test runs. Once every VM is ready, the
   `OrchestratorHandle` is exposed to user test functions with
   `vms[name]` having a bound communicator. Sequential,
   continue-on-failure default.
5. **Cleanup** — LIFO over `state.json` resources. PID-gated so the
   CLI `testrange cleanup <run-id>` refuses if the orchestrator is
   still alive. Terminates the run in `phase=done` (state dir removed)
   or `phase=leaked` (`--leak-on-failure` retained the range; the user
   runs `testrange cleanup <run_id>` later).

## Stovepipe rule

Builders, Communicators, and Credentials never know about each other.
The **Orchestrator** is the broker: it pulls `builder.credentials` and
hands the right one to the Communicator at bind time. Each
Communicator's `bind()` has its own signature — there is no uniform
handle. The orchestrator dispatches by communicator type and supplies
the inputs each kind needs.

## Cache lifecycle

```
testrange cache add <path-or-url> --name debian-13
  ⇒ sha256 of content
  ⇒ store at $XDG_CACHE_HOME/testrange/isos/<sha>.bin + <sha>.json sidecar

CacheEntry("debian-13") in Plan
  ⇒ orchestrator resolves at preflight
  ⇒ fails loud at preflight if missing (clear "testrange cache add ..." hint)
```

## State + cleanup invariants

- **Record-before-create**: a resource is in `state.json` BEFORE the
  driver create-call. A `kill -9` between record and create leaves
  enough information for `testrange cleanup` to act by deterministic
  backend name.
- **Deterministic naming**: `driver.compose_resource_name(run_id,
  kind, name)` is pure. Cleanup never needs the original Plan.
- **Stable MACs**: `driver.compose_mac(plan_name, vm_name, nic_idx)`
  is pure. Per-driver, with the right OUI. Required because
  cloud-init's rendered network-config on the cached disk can be
  MAC-keyed.
- **PID-gated cleanup**: a sibling `state.pid` file records the owning
  process. `testrange cleanup` refuses to act on a run whose PID is
  still alive.

## Subprocess discipline

`testrange` itself runs no subprocesses. Every operation has a Python
library (the backend SDK, paramiko, pycdlib, urllib, cryptography).
Ruff's `flake8-tidy-imports` banned-api blocks `import subprocess` at
lint time and a CI test enforces the same.

What a backend's own service does on our behalf (e.g. libvirtd invoking
`qemu-img`, a hypervisor flattening a disk during a full clone) is that
service's business — the ban is on `subprocess` from `testrange/` code.

If a future feature requires a subprocess directly from Python (cross-
format disk conversion when ESXi/Hyper-V land, for example), it gets
its own ADR and a single sanctioned module at that time.
