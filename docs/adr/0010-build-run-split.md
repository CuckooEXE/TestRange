# ADR-0010: Build/run are two phases, two CLI verbs; disks are cache artifacts; no backend-side reuse

Status: Accepted
Date: 2026-05-22

Amends [ADR-0008](0008-driver-abc-multi-backend.md) (driver disk surface).
Extends [ADR-0005](0005-osdrive-distinct.md) (data disks become build
artifacts) and [ADR-0007](0007-deterministic-config-hash.md) (the hash keys
the *disk set*, not one disk).

## Context

The expensive part of a range is provisioning a guest — boot, run the
install payload, power off. Today that work lives in an "install phase"
(`testrange/orchestrator/install_phase.py`) that is welded to the run phase
inside one `Orchestrator.__enter__`: you cannot warm the cache without also
running tests, and the run phase silently depends on pools the install phase
left behind.

We want to split the lifecycle cleanly so it can be split at the CLI too:

- `testrange build <plan>` — provision every VM to completion, capture the
  resulting disks into the cache, run **no** tests. Warms the cache.
- `testrange run <plan>` — bring the range up from cached disks and run tests.

The reshape also lets us settle several things the current code leaves
implicit or unimplemented: backend-side reuse of base images, data-disk
provisioning (declared but never created — `VMSpec.data_drives` exists and is
only ever *printed*), where `size_gb` is honored (nowhere today), and what
"sidecar ready" means (nothing — readiness is currently absorbed by
`discover_ip` swallowing `GuestAgentError`).

The rename is part of the decision: "install" → "build" throughout
(`PHASE_INSTALL`, `install_phase`, `install_one_vm`, the `_post_install_<hash>`
cache prefix, `post_install_paths`, `install_timeout_s`, `install_uplink`,
`_install_switch`, the `INSTALL_*` consts). "Install" is one builder's verb
(CloudInit); "build" is the phase.

## Decision

### 1. Two phases, two entry points; `run` auto-builds on miss

`build_phase(ctx)` and `run_phase(ctx)` are independent. `build` produces only
cache artifacts (local, plus the HTTP tier when configured). `run` consumes
them. The `Orchestrator` composes both for the test-runner path; the CLI
exposes each as a verb. Both phases are state-tracked (ADR-0003) and fully
self-cleaning.

`testrange run` **auto-builds** any artifact that misses the cache — it is
`build_phase` (over the missing VMs only, per §2) followed by `run_phase`, so
`run` always works against a cold cache. `testrange build` is the run-free
prefix that warms the cache and stops. A `--require-cache` flag on `run` makes
it fail fast on a miss instead of building, so CI can keep build and run as
distinct, auditable invocations.

### 2. The build phase checks the cache before building anything

Today the build pool, switch, and sidecar come up *before* the per-VM cache
probe, so a 100%-hit run pays full sidecar bring-up for nothing. Invert it:

1. For every VM, resolve the base (`cache.resolve(builder.base)`) and compute
   `builder.config_hash(...)`, then probe the build cache.
2. Collect the misses.
3. **Only if there is at least one miss** stand up the build pool / switch /
   sidecar, and loop over *only the missing VMs*.

We keep `base_sha` in the key (ADR-0007) and accept its cost: computing the
key requires resolving the base, which on a cold local cache means a download
even when the *built* artifact turns out to be cached upstream. Correct
invalidation when the upstream image changes is worth one resolve. This is a
deliberate penalty, not an oversight.

### 3. No backend-side reuse: push per VM, delete everything

Reaffirms ADR-0008 invariant #4 (caching lives runner-side, never on the
backend) and takes it to its conclusion:

- **No base sharing.** Two VMs off the same `debian-13` get two independent
  pushes of that image. We do not deduplicate on the backend.
- **No clone-from-base.** The OS disk is materialized by pushing image bytes
  straight to *that VM's* disk volume (§6), not by uploading a shared base and
  cloning an overlay off it.
- **`ensure_base_in_pool` and `create_disk_from_base` are removed** — both the
  `provision.py` helper and the driver method. The replacement is
  `upload_to_pool` directly onto the VM's own disk ref.
- **Everything on the backend is deleted** when it is no longer needed: each
  build VM and its disks immediately after capture; the build pool / switch /
  sidecar at build-phase end; all run resources at teardown. The backend holds
  no testrange state between phases.

This is knowingly less efficient (redundant pushes, no overlay COW). The
simplicity — a backend that is pure scratch space, never a cache — is the
point.

### 4. Every writable disk is a build artifact

A VM is provisioned as a unit: the build VM boots with **all** its writable
disks attached — the OS disk and each `HardDrive`, blank and sized — and the
install payload populates them (e.g. a 100 GB data disk seeded with web
content). On power-off, the orchestrator downloads **every** writable disk and
adds each to the cache as its own entry. The run phase pushes each back and
attaches all of them.

Consequently:

- `config_hash` keys the **disk set**, not one disk: the data-disk
  declarations (count and `size_gb`, in spec order) are folded into the hash,
  because they change the artifact set the build produces. This extends
  ADR-0007's "sensitive to everything that changes the disk."
- Each artifact is named per role: `_built_<config_hash>__os`,
  `_built_<config_hash>__data0`, `_built_<config_hash>__data1`, … A partial
  cache (OS present, a data disk missing) is a miss for the whole VM.

### 5. Built disks are cached locally and pushed upstream when configured

When a disk is downloaded off the backend it is `cache.add`-ed locally **and**,
if an HTTP tier is configured, pushed to it (`manager.push`). The local cache
is always authoritative; the upstream push is best-effort and is what makes a
shared build farm useful. (Today `cache.add` is local-only; the push is new
behavior, gated on a configured HTTP tier.)

### 6. OS-disk origin is a Builder concern; CloudInit is image-based

How the OS disk comes to exist depends on the builder, not the orchestrator:

- **Image-based (CloudInit, the only v0 builder):** the base image *is* the OS
  disk. The orchestrator `upload_to_pool`s the base bytes onto the VM's OS
  disk ref, `resize_volume`s it up to `os_drive.size_gb`, and boots; cloud-init
  `growpart`/`resize2fs` expands the rootfs on the build boot. The captured
  disk is already full-size, so run VMs need neither a seed nor a resize.
- **Installer-based (ESXi Kickstart, Windows autounattend — future):** the OS
  disk is created **blank** at `size_gb`, the install media is attached as boot
  media, and the installer partitions and writes the OS. There are no "OS
  files" to copy onto a disk in the image-based world — that model only exists
  for installer-based builders.

We do **not** introduce a `Builder.materialize_os_disk()` abstraction now (no
speculative abstraction): v0 hard-codes the image-based path and CloudInit is
the only builder. When the second (installer-based) builder lands, OS-disk
origin moves behind a builder-owned method and this ADR is superseded for that
clause. The seam is named here so the build phase is not painted into the
clone-from-image corner.

### 7. Driver disk surface (amends ADR-0008)

- **Removed:** `create_disk_from_base` (the clone-from-base primitive — no
  longer any sharing or overlay to clone).
- **Added:** `create_blank_volume(ref, size_gb)` — provision a blank sized
  volume (data disks at build; installer-based OS disks later).
- **Added:** `resize_volume(ref, size_gb)` — grow a volume to a target size
  (image-based OS disk before the build boot).
- **Unchanged:** `upload_to_pool` / `download_from_pool` (the host↔pool byte
  channel; now the *only* way a disk's content reaches the backend) and
  `write_to_pool` (seed/ISO bytes).

Net: one primitive removed, two added; semantics get simpler (no pool→pool
copy exists anymore — every disk arrives by host→pool upload).

### 8. Sidecars require a native guest agent; "ready" is defined

Design decision: **every sidecar runs a native guest agent.** The
orchestrator never routes IP traffic to a sidecar to manage it — it drives the
sidecar entirely through the driver's native guest channel, sidestepping any
runner↔sidecar reachability problem. Accordingly the run phase gains an
explicit gate after `materialize_sidecar_for`: a sidecar is **ready** when its
native guest agent is connected **and** the orchestrator has successfully read
back the rendered config files (`dnsmasq.conf`, leases path, etc.) it
delivered. The phase blocks on readiness before starting any user VM, so DHCP
is being served before a guest can ask for a lease. (Today there is no
readiness gate; `discover_ip` absorbs the race by retrying on
`GuestAgentError`.)

### 9. Ephemeral build infra; run phase owns the user's pools

The build phase uses a **single dedicated build pool** (a namespace per
ADR-0008 §5, not provisioned storage) plus one switch + one sidecar + the
`build_uplink`, and tears **all** of it down at phase end — including the pool.
The build phase no longer creates the user's declared pools.

This makes pool creation a **net-new step in the run phase**: because build no
longer leaves pools behind, `run_phase` must create the user's `hyp.pools`
itself before pushing disks. (Today only the install phase creates pools and
they survive into run — that dependency is severed.)

`wait_builder_ready` is unchanged and remains the per-VM guest-liveness gate
("bound and pinged" is exactly what its readiness command already does
through the bound communicator).

## Consequences

- **CLI:** `testrange build` and `testrange run` become first-class verbs.
  `build` is run-free cache warming; `run` auto-builds missing artifacts then
  brings the range up (`--require-cache` opts into fail-fast).
- **Cache:** build outputs populate the shared HTTP tier (when configured),
  turning `build` into a build-farm primitive.
- **Cost:** redundant base pushes and no COW overlays. Accepted for a
  stateless backend.
- **`size_gb` becomes load-bearing** for the first time (resize + growpart on
  the build boot) — it was inert before.
- **Data disks work** for the first time — declared, built, cached, restored.
- **Driver authors** implement `create_blank_volume` + `resize_volume` and
  drop `create_disk_from_base`. `MockDriver` is updated first as the reference.
- **Failure is resumable:** each VM's disks land in the cache before the next
  VM builds, so a build that dies on VM 3 of 5 leaves 1–2 cached; the next
  `build`'s up-front probe (§2) skips them and rebuilds only 3–5.
