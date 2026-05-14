# TestRange Research

Open design notes — strategy, architecture sketches, and "what we'd do if/when
X lands" — distinct from `PLAN.md` (which is the agreed-upon design for what
we are building) and `TODO.md` (which is the work queue). Items here are
**candidates**: they capture reasoning and a recommended direction, but
nothing in this doc is committed code until it shows up in `PLAN.md`.

---

## DHCP on hypervisors without built-in DHCP (ESXi)

### Context

`Network.addressing` is a per-Network knob with three modes:

- `"auto"` (default) — orchestrator deterministically picks static IPs for
  any NIC without `ipv4`. Same wire semantics on every driver (no DHCP
  traffic; the guest comes up with a static address baked into its netplan).
- `"dhcp"` — **real DHCP on the wire**. The guest's `dhclient` runs, emits
  requests, honors options, renews. This is the mode plans use when they're
  testing DHCP-client behavior, rogue-DHCP scenarios, DHCP fingerprinting, or
  pcap-based assertions that DHCP traffic appears.
- `"manual"` — every NIC must declare `ipv4`; preflight rejects any that
  don't.

Libvirt fulfills `"dhcp"` for free via the embedded dnsmasq in each rendered
`<network>`. ESXi has no built-in DHCP — vSwitches and port groups are pure
L2 forwarders. This section is the strategy for fulfilling `"dhcp"` on
drivers that can't render it themselves.

`"auto"` is not in scope here; it works natively on every driver because the
addresses are picked at preflight and baked into the guest's staged netplan.

### Recommended approach: per-Network dnsmasq sidecar

**One sidecar VM per `Network(addressing="dhcp")`.** Not per Switch, not a
full router. Smallest unit of opt-in: each sidecar lives entirely within one
subnet and has exactly one job (DHCP, optionally DNS).

#### Sidecar image

Alpine + dnsmasq, pre-built and distributed as a `CacheEntry`. ~150 MB qcow2,
boots in seconds. Users acquire it once via `testrange cache add <url>
--name testrange-dhcp-sidecar`; the existing HTTP cache tier means CI runs
fetch it transparently.

#### Per-instance config

Orchestrator renders a `dnsmasq.conf` (range, lease time, gateway, domain)
into the sidecar's cloud-init seed via `write_files` — the same staging
pattern used for guest netplans. The sidecar's own NIC is static at
`network_address + 1` (the gateway address), set via its own staged netplan.
Same plumbing, different `write_files` payload.

`config_hash` inputs for the sidecar:

- `dnsmasq.conf` bytes (CIDR, range, options).
- Sidecar's own netplan (gateway IP, prefix).
- Base CacheEntry sha.

Cache-stable across re-runs of the same plan, and shared across plans that
declare the same `(CIDR, options)` combo.

#### Where it lives in the Plan

The user **does not declare** the sidecar. `addressing="dhcp"` implies it.
The sidecar:

- Doesn't appear in `orch.vms` (it's infrastructure, not a test target).
- Is recorded in `state.json` as kind `"sidecar_vm"` so LIFO teardown walks it.
- Surfaces only in `describe` output (e.g., `netA: dhcp via sidecar`) and
  `cleanup --dry-run` listings.

API stays: `Network("netA", "10.0.0.0/24", addressing="dhcp")`. No
`Switch(dhcp_sidecar=True)` knob; the addressing mode is the whole
declaration.

#### Naming + MAC

- Backend name: `__sidecar_dhcp_<switch>_<net>`, composed via
  `compose_resource_name` like any other backend object.
- MAC: composed via `compose_mac` keyed on
  `(plan_name, sidecar_name, nic_idx=0)`. Stable across re-runs.
- IP: `network_address + 1`. Same address every static-IP guest's netplan
  already points to as gateway/nameserver, so the convention lines up.

### Discovery is not the sidecar's job

This is the key architectural bit: **`_discover_ip` does not query the
sidecar's lease table.** It asks the *guest* its IP via the driver's
agent channel — VMware Tools on ESXi (`GuestInfo.ipAddress`),
qemu-guest-agent on libvirt.

Two reasons:

1. The orchestrator host may not be on the lab subnet — reaching the sidecar
   over SSH to grep `/var/lib/misc/dnsmasq.leases` introduces a routing
   problem we don't want to inherit.
2. Discovery via guest-agent works identically across `auto`, `dhcp`, and
   `manual` addressing. One code path, one mental model. The sidecar's job
   is to make wire-level DHCP happen; reporting what got assigned is the
   guest's job.

This means landing a QGA / VMware-Tools communicator becomes a prerequisite
for the ESXi DHCP path. That abstraction is wanted independently. The
existing libvirt-lease polling stays as a fallback for guests without an
agent.

### Lifecycle

- **Preflight:** if any Network has `addressing="dhcp"` on a driver that
  doesn't render DHCP natively, the sidecar `CacheEntry` must resolve.
  Standard `cache_miss` finding with `fix_hint`.
- **Install phase:** for each such Network, install the sidecar before any
  user VM on that switch. Install is the standard pipeline: seed → boot →
  `poweroff` → snapshot to cache. Sidecars come up first so dnsmasq is
  listening when user VMs request leases.
- **Run phase:** sidecar comes up, gets its static gateway IP, dnsmasq
  binds to its interface. Then user VMs boot; their `dhclient` finds the
  sidecar.
- **Cleanup:** LIFO from `state.json`. User VMs tear down first; sidecar
  last. Driver's `destroy()` dispatches `sidecar_vm` → `destroy_vm`.

### Driver capability flag

Drivers declare whether they can render DHCP natively:

- `LibvirtDriver.renders_dhcp = True` — dnsmasq is embedded; no sidecar.
- `ESXiDriver.renders_dhcp = False` — orchestrator materializes a sidecar
  per `addressing="dhcp"` Network.

Orchestrator's install phase consults the flag. Sidecars are invisible on
drivers that render DHCP themselves.

### Out of scope (follow-ups)

- **Combined DHCP + NAT sidecar** for `Switch.internet=True`. The lean
  sidecar is DHCP-only. ESXi labs needing both currently get DHCP from the
  sidecar and internet from a separately-configured uplink (the
  `ESXiHypervisor(uplink=...)` knob). Combining is the same project as the
  existing `Switch(gateway=True)` TODO — design fresh when that lands.
- **Advanced DHCP knobs** (`Network(dhcp_range=...)`,
  `Network(dhcp_options={...})`). Useful for rogue-DHCP / option-injection
  scenarios — exactly the audience for `addressing="dhcp"`. Land when the
  first plan that needs them does.
- **Multi-Network sidecars** (one sidecar serving several subnets per
  switch). Don't optimize. Per-Network footprint is small; per-Switch would
  entangle lifecycle and complicate the dnsmasq config.

### What this depends on

- The `Network.addressing` knob split (`auto` / `dhcp` / `manual`) replacing
  the current `Network.dhcp: bool`. Tracked separately.
- A guest-agent communicator (QGA on libvirt, VMware Tools on ESXi) for
  discovery on driver/guest combinations that can't or shouldn't use
  lease-table polling.
- The ESXi driver itself (currently long-term TODO).
