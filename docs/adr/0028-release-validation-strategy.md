# ADR-0028: 1.0.0 validation strategy — adversarial e2e suite on an unmanaged nested host fleet

Status: Accepted
Date: 2026-06-06

## Context

TestRange has three backends at the threshold of a 1.0.0 release: libvirt
(certified reference, ADR-0019), Proxmox (certified single-node, PVE-CERT), and
ESXi (driver complete, pipeline proven live, cert tail in flight). Each was
certified by driving `examples/capabilities.py` green and pinning that run as a
marker-gated integration test (`tests/integration/test_<backend>.py`).

`capabilities.py` is a **breadth survey** — one VM per capability, each touched
once, the happy path. It answers "does the driver implement the contract." It
deliberately does *not* answer "does the orchestrator hold up under churn,
adversarial topologies, and the edge cases where TestRange's own logic (cache
reuse, teardown, concurrency, snapshot lifecycles, error paths) is most likely
to be wrong." A 1.0.0 — which is also where we freeze the public API — wants the
second answer, and wants it on **all three** backends running the **same**
plans, so a discrepancy is attributable to a driver and not to the test.

Two further problems shaped this:

- **The certification substrate has been the bottleneck, not the code.** ESXi
  cert (ESXI-11/12/13) and the nested-PVE build (BUILD-13) are *environment*
  blocked, not driver-blocked: the bare-metal ESXi host at 40.160.34.83 has no
  VM egress path, so build VMs cannot `apt`. Every backend's cert needs a host
  that (a) we can stand up on demand and (b) has working egress.
- **Validating TestRange with TestRange is circular.** TestRange can already
  host a nested hypervisor (`GuestHypervisor`, ADR-0021). Using that to build
  the hosts under test would fold the system under test into the test harness —
  a bug in nesting could mask or manufacture a discrepancy.

## Decision

Cut a **`REL` release-validation epic** with three pillars, gated on all three
backends passing the same adversarial suite before 1.0.0.

### 1. An adversarial e2e suite in `tests/end-to-end/`, distinct from `capabilities.py`

A new `tests/end-to-end/` tree holds portable, backend-agnostic **Plans**
(`PLAN` + `TESTS`, the same shape as `examples/`), each opening with a docstring
stating **what** it stresses and **why** that edge is failure-prone. They run
the normal way — `testrange run --profile <name> tests/end-to-end/plans/<plan>.py`
— and are *also* wrapped by `test_e2e_<backend>.py` harnesses behind the
per-backend pytest markers (`libvirt`/`proxmox`/`esxi`), gated on
`TESTRANGE_*_PROFILE`, mirroring the existing cert wiring so they run out-of-band
against a live host in CI.

Two tiers:

- **Generic** plans run on *every* backend (lifecycle/power churn, networking
  edge matrices, build/cache reuse, snapshot + memory-snapshot churn,
  concurrency `--jobs`, teardown/leak/cleanup, error paths).
- **Backend-specific** plans (`libvirt_specific.py`, `proxmox_specific.py`,
  `esxi_specific.py`) exercise what only that backend exposes (device
  bus/model concretes, VMXNET3, datastore/vmdk specifics, QGA vs VMware Tools,
  remote `qemu+ssh` gateway).

`capabilities.py` stays **THE** certification survey (CLAUDE.md rule 4 unchanged;
new capabilities still land there first). The e2e suite is the additive,
adversarial layer on top of it — it does not replace it.

### 2. An unmanaged, scripted nested host fleet in `tools/`

A scripted harness under `tools/hypervisor-hosts/` stands up each hypervisor as a
**raw libvirt VM** (`virt-install` + kickstart / answer ISO), **independent of
TestRange** — no `GuestHypervisor`, no TestRange driver. The hosts sit on a
libvirt NAT network (`tr-egress`), which gives their build VMs the egress the
bare-metal ESXi host lacked, **superseding the environment block on
ESXI-11/12/13 and BUILD-13**. The harness emits the `connect.toml` profiles that
bind TestRange to each hosted hypervisor (`esxi-e2e`, `proxmox-e2e`,
`libvirt-remote-e2e`).

This is independent *on purpose*: the host fleet must not share code with the
system under test. The ESXi host reuses the hard-won install lessons recorded
under BACKEND-13 (IDE installer CD on BIOS), BUILD-22 (heredoc terminator), and
ESXI-17 (`%firstboot`), but as a standalone kickstart, not via the driver.

### 3. 1.0.0 = all three backends green + a public-API freeze

The release gate is the full e2e suite passing on hosted libvirt **and** Proxmox
**and** ESXi (run in that-reverse order — ESXi first, the least-proven). Each run
produces a discrepancy report (`docs/dev/e2e-findings-<backend>.md`); every
finding becomes a bug ticket in its swimlane, fixed before the gate clears.

1.0.0 is a genuine SemVer commitment: `major_version_zero` flips to `false` in
`pyproject.toml`, an `/api-diff` baseline is captured, and the public surface
(`testrange.__init__` exports, the driver ABC, the CLI) is frozen against
casual breakage. The cleanup of `PLAN.md`, `TODO.md`, and `docs/` happens
*after* the validation pass, so it reconciles against validated reality rather
than intent.

## Rejected alternatives

- **Push the stress cases into `capabilities.py`.** Conflates two jobs with
  opposite shapes: the survey wants one-VM-per-capability breadth and must stay
  readable as the canonical contract; the e2e layer wants churn, adversarial
  topologies, and repetition. Mixing them makes the survey unreadable and the
  stress coverage shallow.
- **Build the host fleet with `GuestHypervisor` nesting.** Lightest, but folds
  the system under test into the harness — a nesting bug could mask or fabricate
  a discrepancy. The whole point of the validation pass is an independent
  substrate.
- **Ship ESXi as 1.0.0 "beta" with only libvirt+PVE gated.** Rejected: the
  nested host fleet removes the only real ESXi blocker (egress), so "all three
  green" is now an achievable bar, and a 1.0.0 that ships a backend uncertified
  on its own release suite undercuts the API-freeze commitment.
- **A documented manual host-setup runbook instead of scripted tooling.**
  Lighter, but the fleet is rebuilt every regression cycle; a one-off runbook
  rots and is not CI-able. The hosts are infrastructure, so they get code.

## Consequences

- A new `REL` swimlane and epic (REL-1..20): the e2e scaffolding + suite
  (`tests/end-to-end/`), the `esxi` pytest marker, the scripted host fleet
  (`tools/hypervisor-hosts/`), three run-and-report tasks, the docs/PLAN/TODO
  reconciliation pass, and the release cut.
- The ESXi cert tail (ESXI-11/12/13) and BUILD-13 are **unblocked** by the host
  fleet's egress; they fold into the e2e validation rather than waiting on the
  bare-metal host.
- The epic is gated on the in-flight **ESXi Builder** effort (ESXI-16/17)
  landing; the generic suite and the libvirt/Proxmox hosts can proceed in
  parallel with it.
- 1.0.0 freezes the public API. Post-freeze, breaking changes bump the major;
  `major_version_zero=false` makes commitizen enforce it.
