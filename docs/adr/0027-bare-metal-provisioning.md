# ADR-0027: Bare-metal provisioning via an out-of-band controller

Status: Accepted
Date: 2026-06-06

## Context

TestRange's `build` and `run` verbs both assume a **live hypervisor** to bind a
`--profile` against. Nothing produces one. The recurring real-world need is the
step before that: take a physical server with an out-of-band management
controller (HP iLO, Dell iDRAC, or any Redfish BMC) and install a hypervisor
onto it, unattended, so the existing pipeline can target it.

The machinery is almost all present. The installer-origin builders
(`ProxmoxAnswerBuilder`, `ESXiKickstartBuilder`) already emit an auto-install ISO
via the `boot_media()`/`prepare_boot_media()` seam (ADR-0022, BUILD-1), and the
nested-virtualization work (ADR-0021) already installs a hypervisor onto a
*substrate* and then synthesizes a profile against the result. The only genuinely
new thing bare metal needs is a way to put that ISO on a physical box and cycle
its power — i.e. the substrate realizer changes from "an L0 driver's
`create_vm(boot_media_ref=…) + start_vm`" to "a BMC's virtual-media + power
actions."

The design question is how to model that without distorting the existing
abstractions. Several shapes were considered and rejected (see below), the most
tempting being to make the BMC a `HypervisorDriver` so the install rides the
existing `create_vm`/nested recursion. That fails on two counts: a BMC would
`NotImplementedError` the entire driver core (switches/networks/pools/volume
I/O — a physical box hosts exactly one thing, itself), forcing every caller to
special-case it; and re-imaging iron is a slow, destructive, **once-per-server**
operation whose lifecycle is the opposite of `create_vm`'s cheap,
destroy-every-run one. Same signature, opposite semantics — a worse lie than a
missing method.

## Decision

Add a third CLI verb, **`provision`**, and a small subsystem around it.
`provision` installs a hypervisor (or any installer-origin OS) onto physical
hardware via its BMC, then leaves a host the existing `build`/`run` target with a
normal `--profile`. The pipeline is three decoupled verbs, each consuming the
prior's output: `provision` (iron → hypervisor) → `build` (hypervisor → cached
disks) → `run` (cached disks → live range + tests).

`testrange provision <plan> --profile <name> --controller <name>`. `--profile` is
the *post-install* connection (where the host will answer, its API creds, its
uplinks) and feeds the answer file; `--controller [file:]name` resolves a
`connect.toml` section by `driver=` for the BMC, exactly as `--profile` does.

- **The controller is its own ABC, not a `HypervisorDriver`.**
  `OutOfBandController`: `connect`/`disconnect`, `inventory()`,
  `attach_media(url)`/`detach_media`, `set_boot_override(target, persist=)`,
  `power`/`power_cycle`/`power_state`. It knows nothing about
  switches/networks/pools/VMs. `ControllerProfile` mirrors `BackendProfile`
  (scheme `ClassVar` + `_from_table` + `build_controller`, registry dispatch via
  `load_controller`). First and only concrete: **Redfish** (covers
  iLO5+/iDRAC9+/Lenovo XCC/Supermicro X11+), lazy-imported via `_import_redfish()`
  with a `[redfish]` install hint.

- **`ProvisioningPlan(host: HostRecipe)` is nameless.** A populate `Plan` carries a
  name because it namespaces backend resources (`compose_mac(plan_name, …)`,
  per-plan pools) and because teardown must find its resources. Provision has
  none of that — the box *is* the resource, there is no co-tenancy to namespace,
  and there is no teardown (the next provision stomps the disk). The idempotency
  identity is the recipe's `config_hash` stamped into the install, not an
  author-given name.

- **`HostRecipe = spec × builder`** — no communicator. Follow-on configuration is
  baked into the answer file / first-boot script and run by the installer, not
  pushed over a connection afterward, so the run-phase communicator has no role.
  The builder is constructed and injected explicitly; there are **no per-backend
  front-door classmethods**. `HostRecipe` is a `VMRecipe`-shaped leaf and follows
  `VMRecipe`'s explicit-builder convention (`builder=ProxmoxAnswerBuilder(…)`),
  not `GuestHypervisor`'s composite-assembly `.libvirt()`/`.esxi()` doors — those
  earn their keep by wiring a builder + communicator + inner plan + package stack;
  `HostRecipe` has nothing to hide behind a classmethod.

- **`HostSpec` is a requirements contract, not a construction order.** Where
  `VMSpec` *materializes* hardware (`CPU(2)` creates 2 vCPUs, 25 `DataDrive`s
  create 25 vmdks), `HostSpec` *asserts minimums against discovered inventory*. It
  is a scalar `firmware` (top-level, mirroring `VMSpec.firmware`) plus a discrete
  `devices` list of `Required*` entries — `RequiredCPU(cores)`,
  `RequiredMemory(mb)`, `RequiredOSDrive(gb)`, `RequiredDataDrive(gb)`,
  `RequiredNIC()`. They are positional, value-is-a-minimum (the `Required*` prefix
  carries "≥"), and **discrete 1:1** — no `count` (two data drives are two
  entries, which also lets heterogeneous requirements differ). They are
  backend-agnostic — *not* the `Libvirt*`/`ESXi*` device concretes — so the plan
  stays portable. The list is textually identical to a `VMSpec` device list
  except the type names and the assert-vs-construct flip.

- **Inventory matching resolves physical identity; the plan never names it.** The
  provisioner pulls inventory from the controller (`inventory()`) and solves a
  **1:1 bipartite match** of `Required*` entries against physical resources (size
  minimums as edges; deterministic tiebreak — OS = smallest sufficient, data =
  remaining largest-first, stable by hardware id). The constraints usually
  disambiguate themselves; the matcher only tiebreaks the all-identical case. It
  **prints the resolved assignment** ("OS→disk0 …, data→…, untouched: …") before
  any destructive step, and claims **only** the matched devices — undeclared
  hardware is never touched (an iron data-safety rule with no VM analog). There is
  **no author-specified disk identity/selector** in the portable plan.

- **The provisioner orchestrator is separate and lean:** preflight
  (`controller.connect` → `inventory()` → 1:1 match → print assignment →
  idempotency gate via `profile.build_driver().connect()` + version) → build
  (reuse the Builder seam + cache unchanged) → stage (serve the prepared ISO at
  the BMC-reachable `media_url_base`) → realize (`attach_media` →
  `set_boot_override(CD, one-time)` → `power_cycle`) → wait (readiness = the
  profile driver connects, bounded by `--provision-timeout`) → finalize
  (`set_boot_override(DISK, persist)` → `detach_media`). No
  switch/network/pool/nested phase, **no teardown**. It shares the builder/cache
  substrate and the build-result micro-sequence with the run orchestrator and none
  of its phase machinery.

### Rejected alternatives

- **BMC as a `HypervisorDriver`, host as a "VM."** Inverts the driver contract
  (the BMC `NotImplementedError`s the mandatory core) and buries an irreversible,
  once-per-server lifecycle inside `create_vm`/nested teardown — every `run` would
  re-image the box. The honest waist (boot an installer onto a substrate + power
  it) is far narrower than the full driver.
- **An `[<name>.install]` block in `connect.toml`.** The install recipe is
  topology-layer data, not connection-layer data; `connect.toml` answers "how do I
  reach a thing," never "what to install and on which disk." It belongs in a
  portable plan.
- **`--controller` as a flag on `run`.** Re-couples a slow, destructive
  once-per-server operation to a fast once-per-test-run one. The "one command"
  story is `provision && run`, not an overloaded verb.
- **`count=` on a requirement / a `select=` disk selector in the plan.** `count`
  collapses heterogeneous requirements and diverges from `VMSpec`'s discrete-device
  idiom; a selector leaks host-specific physical identity into the portable plan
  and is less safe than auto-match-from-inventory plus a printed plan.

## Consequences

- A new `PROV` subsystem: `ProvisioningPlan`/`HostRecipe`/`HostSpec` + the
  `Required*` vocabulary, the inventory matcher, the `OutOfBandController` ABC +
  `ControllerProfile` registry + a Redfish concrete, the `provision` verb +
  `provision_range()`, and an ephemeral ISO-staging HTTP(S) server. Tracked as
  PROV-1..13.
- **New infra wrinkle:** the staged ISO embeds the baked answer file (root cred),
  so plain HTTP on the BMC LAN is credential-on-the-wire. Mitigate with a one-time
  unguessable URL torn down post-realize, or Redfish-over-HTTPS / CIFS/NFS virtual
  media.
- **Installer-origin only in v1.** Image-origin builders (`CloudInitBuilder`) have
  no "upload to pool" equivalent on iron — realizing them needs a writer stage
  (boot a live/iPXE env, write the image, reboot, the MAAS/Tinkerbell pattern),
  deferred to PROV-14.
- **No disk-identity override** until a concrete box defeats the
  constraints-plus-deterministic match; when it does, the override is host-specific
  and lands at the bind layer (a provision-time `--disk` flag or a `connect.toml`
  map), never in the portable plan (PROV-15).
- `provision` generalizes to non-hypervisor installer-origin OSes (a bare Debian
  box is an end in itself); when the output *is* a hypervisor, `run --profile`
  targets it next.
- Provisioning is a **controller** capability, so it is exercised by its own
  `examples/provision-proxmox.py`, not an `examples/capabilities.py` entry (the
  portable *driver* survey).
