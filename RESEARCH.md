# TestRange Research

Open design notes — strategy, architecture sketches, and "what we'd do if/when
X lands" — distinct from `PLAN.md` (which is the agreed-upon design for what
we are building) and `TODO.md` (which is the work queue). Items here are
**candidates**: they capture reasoning and a recommended direction, but
nothing in this doc is committed code until it shows up in `PLAN.md`.

---

## PVE-16 spike — reading the build-result channel out of a Proxmox guest

> **Finding (2026-05-24, live host `ns1001849`):** serial-console output
> cannot be read out of a PVE guest over plain REST — the only path is
> `termproxy`→`vncwebsocket` (a websocket, i.e. a *second* transport).
>
> **Decision (2026-05-24):** the user accepted that second transport — the
> websocket buys *live* fast-fail + live build output the disk fallback can't.
> So the PVE sink is the **live serial read over `termproxy`→`vncwebsocket`**
> (`PLAN.md` §21, PVE-17). The disk-over-SFTP fallback below stays documented
> but unbuilt. The transport policy now carries a second sanctioned exception
> (see [[project_testrange_proxmox_transport]] / ADR-0008 §6). The analysis
> below is retained as the rationale and as the fallback spec.

### What the spike asked

§21 chose "serial console" as the universal, agent-free, builder-agnostic
vector for the structured build result + log. PVE build VMs already get
`serial0: socket` (`drivers/proxmox/_vm.py:170`). The question: can the
orchestrator *read* that serial output over the proxmoxer-only transport
(proxmoxer REST + the single sanctioned SFTP `download_from_pool` exception)?

### What I found (empirical, against the live API)

Authenticated to the live host with the lab creds (user+password →
`PVEAuthCookie` ticket, which is how the driver authenticates — *not* an API
token) and mapped the console-family endpoints under
`/nodes/{node}/qemu/{vmid}/`:

| Endpoint | GET probe | Meaning |
|---|---|---|
| `termproxy` | HTTP **501** | exists, **POST-only** |
| `serialport` | HTTP 501 | exists, POST-only |
| `vncproxy` / `spiceproxy` | HTTP 501 | exists, POST-only |
| `vncwebsocket` | HTTP 400 | exists, GET = **websocket upgrade**, needs params |

**There is no plain REST GET that returns serial bytes / a console buffer.**
The only path to serial output is the two-step the web UI uses:

1. `POST …/termproxy` → returns `{ticket, port, user}` (proxmoxer *can* do this).
2. Open a **websocket** to `…/vncwebsocket?port=&vncticket=` and consume the
   stream — which proxmoxer **cannot** do (it's a REST client; users bolt on
   `websocket-client` separately). The stream is VNC/termproxy-framed (an
   `RFB 003.008` handshake, a `user@realm:ticket\n` framing, base64 in xterm
   mode) — fragile to parse — and is **stream-only**: there is no history
   buffer, so the consumer must attach *during boot* and hold the socket to
   capture cloud-init output as it streams.

### Why serial-over-REST is rejected (not just hard — wrong for us)

Even though the password-ticket auth sidesteps the documented
"termproxy rejects API tokens" gotcha, adopting it would mean:

- a **new dependency** (`websocket-client`) proxmoxer doesn't provide;
- a **second transport** (a websocket on :8006) — the transport policy permits
  exactly *one* byte-egress exception (SFTP for `download_from_pool`), and the
  client docstring already records that "PVE cannot serve a volume's bytes over
  REST." Serial is the same class of limitation;
- coupling to proxmoxer session internals (extracting the cookie/CSRF) + a
  fragile VNC-frame parser + a live-attach-during-boot requirement.

A full websocket PoC wouldn't change this — the dependency + second-transport +
framing objections are dispositive on their own.

### The viable PVE sink: result disk over SFTP

The guest writes the `TESTRANGE-RESULT:` record (+ framed log) to a small
**ephemeral result disk**; the orchestrator pulls it back via the
**already-sanctioned** `download_from_pool` SFTP path and parses it host-side.
This reuses proven machinery (PVE-3), adds **no** transport and **no** runtime
dependency, and is a deterministic parse. Snapshot-only (read after the guest
powers off), but failure stays fast because a fail-fast provisioning script
powers off promptly — only a true wedge hits the watchdog. The result disk is
**never cached** (seed-ISO lifecycle: created, read back, deleted in teardown).

Format is a PVE-17/BUILD-3 detail, not the spike's: a **raw-offset blob**
(zero deps, fine for the Linux/cloud-init path now) or **FAT-by-label**
(`TRRESULT`; cross-OS — Windows auto-mounts it by letter — and human-
inspectable, needs a pure-Python FAT reader). The spike only had to confirm
byte-egress for the result is solved — it is, by SFTP.

### Architectural correction this forces on §21 / CORE-5

"Serial console as *the* universal vector" was too strong. Precisely:

- **Universal guest-side:** every target OS can *write* the record to a UART
  (`/dev/ttyS0`, `COM1`, …). That half holds.
- **Not universal host-side:** *reading* it differs per backend — libvirt can
  (pty/file), **PVE cannot** practically and reads the same record from a
  result disk instead.

So the universal thing is the **`TESTRANGE-RESULT:` record/protocol**, emitted
to whatever sink(s) the guest can write; the driver capability abstracts the
*host read* and is backed differently per driver (serial-read for libvirt,
disk-read for PVE). Two consequences for the dependent tickets:

- **CORE-5:** name the capability around the *build-result sink*, not
  "serial" (PVE's sink is a disk). It must support a **snapshot read**
  (read-after-finish); live-tail is a libvirt-only optimization, not required
  by any path.
- **BUILD-3:** the builder emits the record to the serial console *and*, when
  the driver provisions a result disk, to that disk. The orchestrator tells the
  builder which sink(s) to target so the builder stays backend-agnostic.

### PVE-18 addendum — termproxy/vncwebsocket framing confirmed (2026-05-24)

De-risked PVE-17's one unknown (the frame unwrap) with a read-only PoC against
the live node-shell termproxy (`websocket-client`). The framing is far simpler
than the forum chatter implied — **termproxy streams raw PTY bytes, not a
VNC/RFB protocol** (the `RFB 003.008` handshake is the `vncproxy`/noVNC
*graphical* path, a different endpoint). Confirmed recipe:

1. proxmoxer auth (user+password) → session ticket + CSRF. Set the session
   ticket as the **`PVEAuthCookie`** cookie yourself — PVE returns it in the
   JSON body, *not* as `Set-Cookie`.
2. `POST …/qemu/{vmid}/termproxy` (CSRF header) → `{port, ticket (="PVEVNC:…",
   the *vncticket*, distinct from the session ticket), user, upid}`.
3. ws connect `wss://host:8006/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket?port={port}&vncticket={urlenc}`,
   headers `Cookie: PVEAuthCookie=<session ticket>` + `Origin: https://host:8006`
   (Origin matters), TLS verify off in lab.
4. On open send one frame `"{user}:{vncticket}\n"` → server replies binary
   `b"OK"`.
5. Thereafter: server output arrives as **binary ws frames of raw bytes** — no
   prefix, no base64. Concatenate payloads = the serial stream; scan for the
   `TESTRANGE-RESULT:` markers. (Client→server *input* would use the
   `"0:<len>:<data>"` channel framing, but build-result reading never sends
   input.)

Notes for PVE-17:
- Idle connections time out; PVE's web client sends a `"2"` ping frame
  periodically. Hold the socket open from `start_vm` through build completion
  and ping during quiet stretches.
- ANSI/control bytes interleave (e.g. `\x1b[?2004h`); the base64-framed
  `TESTRANGE-LOG` payload is robust to that, and the `TESTRANGE-RESULT:` line
  is matched, not parsed positionally.
- termproxy requires **password-ticket** auth (rejects API tokens) — already
  recorded as a driver constraint.
- Validated the *consumer* protocol end-to-end on the node shell; VM-`serial0`
  is the same vncwebsocket consumer (backend pipes `qm terminal -iface
  serial0`). A VM-level confirmation is a nice-to-have, not a blocker.

## DHCP on hypervisors without built-in DHCP (ESXi)

> **Superseded — promoted to `PLAN.md` §10 (2026-05-14).** §10 takes the
> sidecar further: it runs on *every* driver including libvirt (TestRange
> stops using libvirt's embedded dnsmasq), rather than only as the ESXi
> fallback. The `LibvirtDriver.renders_dhcp = True` flag and the "libvirt
> stays native" framing below are no longer the plan. The DHCP/DNS
> sidecar *mechanics* documented here still hold — see §10 for the
> agreed design. (NB: §21 is now "Build result signaling"; this note
> formerly mis-cited it.)

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
