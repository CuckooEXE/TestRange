# ADR-0017: Build VMs get a dedicated build NIC; one match-by-MAC netplan, no run-phase staging

Status: Accepted (core mechanism validated by spike 2026-05-30 — see Validation;
implemented 2026-05-30 in BACKEND-7 / ORCH-9 / BUILD-6)
Date: 2026-05-30

**Amends [ADR-0010](0010-build-run-split.md)** — the build phase no longer
wires the build switch to a VM's declared `spec.nics`; every build VM is given
one dedicated, transient build NIC instead, and its declared NICs are not
attached during build. **Amends [ADR-0006](0006-driver-stable-mac.md)** —
match-by-MAC becomes the sole netplan interface-matching strategy (the
`match: name: en*` belt-and-suspenders is dropped), and the build NIC takes a
reserved MAC slot disjoint from the declared NIC indices. The
[ADR-0007](0007-deterministic-config-hash.md) determinism contract is unchanged
(the install seed shape changes once, forcing a one-time rebuild).

## Context

The build phase attaches the build switch to a VM's **declared** NICs:

```python
network_refs = {nic.network: build_net_backend for nic in spec.nics}
```

(`build_phase.py`; the libvirt driver then emits one `<interface>` per
`spec.nics` in `_vm.py`.) Two failures fall out of that one line:

- **Zero-NIC VMs build with no network.** A VM reached only over the QGA
  `NativeCommunicator` at run time (e.g. a `no-net` VM with zero NICs)
  declares zero NICs, so its build boots with no interface on the build switch.
  Any builder that needs the network dies — `apt-get install qemu-guest-agent`
  exits 100. This is the ORCH-9 finding from BACKEND-1.D libvirt certification;
  it is backend-agnostic and stayed hidden only because `MockDriver` never runs
  real `apt`.
- **A single-static-NIC VM has the same shape.** The install boot can't use the
  declared static address (its real subnet has no route on the build switch), so
  today the install netplan DHCPs the declared NIC by kernel name and the *real*
  static netplan is smuggled in for later.

That smuggling is the deeper cost. Because the build NIC **is** the declared
NIC, that one interface must serve two masters — DHCP for `apt` during build,
the user's static address at run. Since a static address can't be live during
build, the `CloudInitBuilder` carries a whole staging apparatus
(`cloudinit.py`):

- `render_network_config` — an install-only netplan that DHCPs each declared
  NIC, matched by **kernel name** (`en*`, `en1*`, …).
- `_render_run_netplan_write_files` / `_render_run_netplan_yaml` — the *real*
  run-phase netplan, matched by **MAC**, staged via cloud-init `write_files` so
  it overwrites the install netplan later in the same boot.
- `99-testrange-disable-network.cfg` — disables cloud-init's network module on
  later boots so it can't undo the staged file.

So there are two parallel NIC models (name-matched install vs MAC-matched run)
that must stay consistent, and the install path drags the run topology onto the
build switch via positional `en{idx}*` matching that assumes the backend numbers
slots in spec order.

## Decision

**1. Every build VM is provisioned with exactly one dedicated, transient build
NIC on the build switch, independent of `spec.nics`. The VM's declared NICs are
not attached during build.** Build connectivity is uniform — one NIC, one
sidecar-served network — and decoupled from the declared topology. The run phase
is unchanged: it uses `spec.nics` with the real switches, so `no-net` still runs
with zero NICs over QGA.

**2. The build NIC is statically addressed from the build switch's
`NetworkAddressing`.** It takes a reserved slot in the `.3`–`.9` infra range
(`_addressing_consts.py`); when the build switch is `nat=True` the sidecar at
`.1` (`SIDECAR_OFFSET`) is its gateway and DNS, exactly as any declared
`StaticAddr` derives gateway/DNS from its Switch. DHCP remains a valid fallback
for an isolated/no-nat build switch (which cannot egress regardless). Static is
the default: it is deterministic, drops the build VM's dependency on the
sidecar's DHCP (the sidecar is still needed for NAT/DNS), and removes a
lease-discovery step from the build path.

**3. The cloud-init `network-config` is the single, final netplan.** It contains
the build NIC plus every declared NIC, all matched by MAC, and is applied live on
the build boot. The same file persists into the cached image unchanged. At run,
the build NIC's MAC is absent so its stanza is inert; the declared NICs come up
with their baked addresses. During build the inverse holds: the declared NICs are
physically absent, so *their* stanzas match nothing and are inert — no route
conflict, no carrier-wait hang — and `apt` egresses via the build NIC.

**4. The staging apparatus is retired.** `render_network_config`,
`_render_run_netplan_write_files`, and `_render_run_netplan_yaml` collapse into
one match-by-MAC renderer used directly as `network-config`. The
`99-testrange-disable-network.cfg` write-file is **kept** — it is what pins the
build-boot-rendered netplan across the seed-less run boot (validated below) and
is a single small file, not the two-file staging dance. So the net change is:
one renderer instead of two, the netplan delivered *as* `network-config` instead
of smuggled via `write_files`, and the disable guard retained as-is.

**5. The build NIC gets a reserved MAC slot.** `compose_mac` is given a sentinel
NIC index disjoint from the declared indices (`0..n-1`), so the build NIC's MAC
never collides and never enters the declared-NIC MAC tuple that feeds the run
netplan and `config_hash`. Match-by-MAC is now the sole strategy; the
name-pattern fallback in both the install and run renderers is removed.

## Validation

Validated 2026-05-30 by a throwaway hand-rolled libvirt spike (no testrange
code), on the project's own base image (`debian-13-generic-amd64.qcow2`), under
`qemu:///session` with SLIRP user-mode networking. One disk, two boots, one
cloud-init `network-config` carrying both a `build0` (DHCP) and a `data0`
(static `10.99.0.50/24` + default route) stanza, matched by MAC, plus the
`99-disable-network.cfg` guard:

- **Build boot** — only `build0` attached. `data0`'s static stanza matched no
  interface and was inert; `network-online.target` was reached with no stall;
  egress over `build0` worked. cloud-init rendered the combined config verbatim
  to `/etc/netplan/50-cloud-init.yaml`.
- **Run boot** — seed detached, `build0` removed, `data0` attached. `data0` came
  up with its static address by MAC; `build0`'s stanza was now the inert one (no
  hang); and the rendered netplan persisted **unchanged** across the seed-less
  reboot (no cloud-init re-render). The static-build-NIC variant (§2) is covered
  transitively — a static on a *present* NIC is exactly what `data0` did here.

The one behaviour the design assumed but cloud-init's docs don't make ironclad —
inertness of an absent-MAC stanza + netplan persistence across the seed-less
boot — therefore holds on the real guest stack.

## Consequences

- ORCH-9 is fixed, and zero-NIC and single-static-NIC VMs build uniformly with
  the same one-NIC build topology.
- Build connectivity is decoupled from declared topology. The baked
  match-by-MAC netplan becomes the single source of truth for run-time
  addressing; there is no install-vs-run divergence and no positional
  name-matching.
- Run-time IP assignment stays where it already is — nowhere. Statics are baked
  into the image; `discover_ip` only *reads* `StaticAddr.host` to know where to
  SSH (`run_phase.py`).
- cloud-init now applies `network-config` live during build (it was deferred).
  This is safe precisely because the declared NICs are absent during build.
- One-time `config_hash` churn: the install seed shape changes, forcing a full
  rebuild on first run after this lands. The ADR-0007 determinism contract is
  otherwise unchanged.
- **ORCH-4 (parallel build pass) interaction:** serial build uses one fixed
  build-NIC slot safely; concurrent builds need a distinct build IP per
  in-flight VM, allocated from the `.3`–`.9` infra range (capping concurrency)
  or a widened range. Recorded so it is not a surprise when ORCH-4 lands.
- **Verification gate — satisfied** (see Validation). The rendered netplan
  survives the seed-less run boot, and an absent-MAC stanza is inert in both
  phases, so the design's one non-obvious assumption is confirmed rather than
  assumed.
