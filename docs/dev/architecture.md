# Architecture and Design

`testrange` is a declarative test-range orchestrator: a user writes a
`Plan` (a Python dataclass tree), hands it to the orchestrator, and
the orchestrator brings up VMs against a hypervisor, runs user test
functions, and tears the range down.

## High-level shape

```
Plan(LibvirtHypervisor(connection, networks, pools, vms=[VMRecipe(...)]), name=)
                                                       │
                                                       ▼
                          Orchestrator
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
       CacheManager   HypervisorDriver  StateStore
       (local-only,   (registry; only   (state.json
        for now)       LibvirtDriver     + state.pid)
                       today)
```

## Key components

- **`Plan(*hypervisors, name=)`** — the top-level user declaration.
  Currently exactly one hypervisor; the variadic shape is locked in for
  future multi-hypervisor without changing the call shape.
- **`LibvirtHypervisor(connection=, networks=, pools=, vms=)`** —
  the libvirt-flavored top-level entry. The driver is constructed from
  this type via the driver registry (`testrange.drivers.driver_for`).
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
- **`HypervisorDriver`** ABC — connect, preflight, network/pool/VM CRUD,
  stable MAC derivation, DHCP lease lookup, volume transport (see
  Pool I/O below). Concretes register themselves with the driver
  registry at import time. Today: `LibvirtDriver`.
- **Pool I/O** — `upload_to_pool` (host file → in-pool volume) and
  `download_from_pool` (in-pool volume → host file) both flow through
  the driver's stream API. The orchestrator never opens pool files
  directly. `download_from_pool` flattens copy-on-write chains before
  streaming so cached disks are self-contained. The pool root is
  driver-chosen and may be URI-aware (LibvirtDriver picks
  `/var/lib/libvirt/images/testrange` for `qemu:///system`,
  `~/.local/share/testrange/pools/` for `/session`).
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

1. **Pre-Flight** — read-only checks (subnet overlap, cache
   resolvability, pool-root writable). Returns `PreflightReport`.
   Errors abort before any state.json write.
2. **Install** — per-VM, builder-driven, cache-aware. Cache hit on
   `builder.config_hash(...)` skips the build. Cache miss brings up
   a transient install VM on a transient internet-NAT network with
   the cloud-init seed; polls power-state until the VM
   self-terminates via `runcmd: [..., poweroff]`; snapshots the
   post-install disk into the cache; tears down the install VM.
3. **Run** — user networks created; each VM gets a fresh overlay off
   the cached post-install disk; defined + started with no seed.
4. **Test** — communicators are bound (discovered IP per VM), then
   each builder's readiness check (`builder.wait_ready_argv`) runs
   against the bound communicator; a non-zero exit raises
   `BuildNotReadyError` before any test runs. Once every VM is ready,
   the `OrchestratorHandle` is exposed to user test functions with
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
library (libvirt-python, paramiko, pycdlib, urllib, cryptography).
Ruff's `flake8-tidy-imports` banned-api blocks `import subprocess` at
lint time and a CI test enforces the same.

libvirtd itself invokes `qemu`, `qemu-img`, `dnsmasq`, etc. — that's
libvirtd's business. In particular, `LibvirtDriver.download_from_pool`
flattens via `pool.createXMLFrom`, which internally runs
`qemu-img convert` inside libvirtd. The ban is on `subprocess` from
`testrange/` code, not on what libvirtd does on our behalf.

If a future feature requires a subprocess directly from Python (cross-
format disk conversion when ESXi/Hyper-V land, for example), it gets
its own ADR and a single sanctioned module at that time.
