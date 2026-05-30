# ADR-0014: The build switch is user-declared; `ManagedBuildSwitch` manufactures fenced egress

Status: **Superseded by [ADR-0016](0016-named-uplinks-out-of-band-egress.md)** (2026-05-29)
Date: 2026-05-26

> **Superseded.** `ManagedBuildSwitch` and the manufactured/fenced egress segment
> are removed. Egress is now an out-of-band host bridge TestRange merely attaches
> to via a NAT `Sidecar` (named by an `[uplinks]` profile entry); the build switch
> is a plain portable `Switch | None` on the `Hypervisor`. The §1 placement
> argument ("build egress is a binding concern") is reversed by ADR-0016: it held
> only because the uplink was host-specific, which is no longer true. The rest of
> this ADR is retained for history.

Supersedes [ADR-0013](0013-switch-sidecar-split.md) §3 ("The driver/orchestrator
boundary is unchanged") — the build phase no longer synthesizes its switch from
`Hypervisor.build_uplink` / `build_uplink_addr`; those env-knobs are removed.
Relates to [ADR-0010](0010-build-run-split.md) (the build phase that brings the
switch up), [ADR-0008](0008-driver-abc-multi-backend.md) (the driver realizes
the switch's L2; managed egress is a new per-backend capability), and
[ADR-0012](0012-serial-build-result.md) (the watchdog that makes the dropped
coherence check safe).

## Context

The build phase brings up its own transient switch before the build VMs boot
and tears it down LIFO at phase end (ADR-0010 §9). Today that switch is
*synthesized* from two env-knobs on the concrete `Hypervisor` (PLAN §10):

```python
Switch(cidr="10.97.99.0/24",
       uplink=hyp.build_uplink,
       sidecar=Sidecar(dhcp=True, dns=True, nat=True, addr=hyp.build_uplink_addr))
```

Three problems with that shape:

- **The build switch is invisible and unconfigurable.** The test author can't
  pick its CIDR, can't bring their own switch, and can't express the legitimate
  "no sidecar — the builder carries its own static L3" case (the future
  installer-based builders, BUILD-1/BUILD-2). The one knob that *is* exposed,
  `build_uplink`, names a backend bridge — so it was never portable topology.
- **The single-public-IP recipe is a manual, per-backend dance.** Pointing
  `build_uplink` at an internal bridge the host NATs out its real NIC, then
  setting `build_uplink_addr` so the sidecar's `eth1` is static, is a documented
  multi-step setup (`examples/px_hello.py`'s `vmbr9`). Every backend
  reimplements the bridge-plus-host-NAT-plus-fence wiring by hand.
- **Two loose env-knobs encode one relationship.** `build_uplink` and its static
  `build_uplink_addr` only ever make sense together — the same flat-field smell
  ADR-0013 removed from the runtime `Switch`. They want to be one object.

**Unifying mental model.** A build network is an *isolated segment with a single
sanctioned exit to the internet* — ESXi internal-port-group plus NAT-uplink
semantics: "isolated to only what is on the switch, plus internet egress." That
is a **default-deny posture**, not a service blocklist: it states what the
segment is *allowed to reach*, not a list of named services to forbid.

## Decision

**The build switch becomes user-declared, typed `Switch | ManagedBuildSwitch`,
replacing the `build_uplink` / `build_uplink_addr` env-knobs. An opt-in
`ManagedBuildSwitch` manufactures and fences the egress segment; the sidecar is
never abandoned.**

### 1. The build switch is user-declared

The build switch is a value the author hands to the binding, not something the
orchestrator fabricates from env-knobs. Its type is `Switch | ManagedBuildSwitch`.

**Placement** (resolved with the user, 2026-05-26): its end-state home is the
`BackendProfile` / `ResolvedBackend` **binding**, *not* the generic topology
`Hypervisor`. Build egress is a backend-specific concern — `ManagedBuildSwitch(
uplink="vmbr9")` names a backend bridge — so it sits with connection and the
other binding-level knobs, consistent with the CORE-7/CORE-9/CORE-10 split. The
generic `Hypervisor` (CORE-7) carries only portable topology
(`networks`/`pools`/`vms`). Phasing: NET-11 lands `build_switch` on the concrete
`*Hypervisor` where `build_uplink` lives today (like-for-like, ships
independently); CORE-10 then relocates it onto `ResolvedBackend`, collapsing the
two retired env-knobs into one field.

### 2. The sidecar is always present — the cross-backend constant

On **every** backend, Managed or not, the build switch carries a sidecar that
serves DHCP/DNS (and its own NAT) to the build VMs on its `eth0` (switch) side.
We never lean on a backend's bundled DHCP/DNS for the build VMs. The payoff is
concrete: **zero dependency on Proxmox's tech-preview SDN DHCP** (PVE-36) — the
sidecar owns lease/resolver duty uniformly, exactly as it does for runtime
switches (ADR-0013). The sidecar VM, image, and config-ISO contract are
unchanged.

### 3. `ManagedBuildSwitch` — a sibling type that manufactures egress

```python
ManagedBuildSwitch(uplink: str, cidr: str = <default>)
```

A **sibling** type, not a `Switch` subclass: it expresses an *intent the driver
realizes* (manufacture an egress segment and fence it), gated by the driver
capability `supports_managed_build_egress` and preflight-rejected where
unsupported. It does **not** replace the sidecar; it manufactures **only** the
sidecar's uplink (`eth1`) egress segment. The topology is identical to today's
manual `vmbr9` case, in two segments:

- **Switch segment (`eth0`)** — build VMs plus the sidecar at `.1`; the sidecar
  serves DHCP/DNS/NAT; internal and isolated; the sidecar is the only way off
  it. This is an ordinary sidecar'd switch — nothing here is "managed".
- **Egress segment (`eth1`)** — the *only* thing `ManagedBuildSwitch` builds.
  The backend SNATs it to the internet and fences it (§5). The sidecar's `eth1`
  takes a **static** address TestRange assigns per the addressing convention:
  `.1` = node/gateway, `.2` = sidecar `eth1`.

So `ManagedBuildSwitch` automates, uniformly across backends, what was three
manual steps: the per-switch static uplink address (NET-7/NET-8), the host NAT,
and the fence.

### 4. A plain `Switch` build switch is BYO; the sidecar is optional

When the build switch is a plain `Switch`, the author owns its shape and the
sidecar is **optional** — a builder may carry its own static L3 and need no
DHCP/DNS (the installer-based builders, BUILD-1/BUILD-2, are exactly this).

The orchestrator does **not** police whether the switch's services match the
builder's needs. The boundary stands: the **builder** owns guest L3, the
**Switch/Sidecar** owns wire services, the **orchestrator** brokers — it does
not predict one side's needs from the other. A genuine mismatch (a build VM that
gets no address it can use) fails **loud** via the serial build-result watchdog:
`build_timeout_s` (default 600s) is silent-guest-safe by the empty-heartbeat ABC
contract (ADR-0012, confirmed in ORCH-8), so a network-starved build trips the
timeout rather than hanging. This is documented in `docs/user`, not enforced in
code.

### 5. Isolation posture: default-deny on the egress segment

The fence on the egress segment is default-deny:

- **Allow** intra-subnet traffic (stays on the bridge — the sidecar must reach
  the build VM), established/related, and any destination **not** in RFC1918
  (i.e. the public internet).
- **Drop** everything else — host management, the host LAN, other switches.

Intra-switch traffic is intentionally permitted (the sidecar-to-VM path); the
denied set is private/host-facing networks. This is the "isolated plus internet
egress" model stated as a posture, not a list of named services.

### Intent vs. realization (ABC-level)

`ManagedBuildSwitch` expresses intent; each driver realizes it natively. The
capability `supports_managed_build_egress` (registered alongside the existing
per-driver capabilities, CORE-8) gates it:

| Backend | `supports_managed_build_egress` | Realization |
| --- | --- | --- |
| **Proxmox** | `True` | SDN simple-zone VNet with `snat=1` (no DHCP on it) + PVE firewall on the VNet; sidecar `eth1` static. REST-native, proxmoxer-only (PVE-36 confirmed `snat=1` viable; PVE SDN DHCP is tech-preview, so we do not use it). |
| **libvirt** (BACKEND-1) | `True` | NAT network, *or* pyroute2 bridge + host `nftables` MASQUERADE; `nwfilter`/`nftables` fence; sidecar `eth1` static (libvirt's own dnsmasq demoted/optional). |
| **ESXi** (BACKEND-2) | `False` | No host-NAT primitive → `ManagedBuildSwitch` is preflight-rejected. A plain `Switch` on a real port-group uplink yields the same isolation. |
| **Hyper-V** (BACKEND-3) | candidate | Likely supported via `New-NetNat` + an Internal vSwitch + Windows firewall; evaluated at impl. |

The driver names every bridge/VNet/SDN object; the orchestrator never does
(ADR-0008 §1). There is no backend-native DHCP/DNS anywhere — the sidecar owns
those uniformly.

### Preflight

- **Kept:** the `supports_managed_build_egress` capability check; that the named
  `uplink` exists on the backend; that the build switch's CIDR/name does not
  collide with the runtime networks.
- **Dropped:** builder-need prediction (the old DHCP/DNS-coherence check). ORCH-8
  confirmed the `build_timeout_s` watchdog catches a network-starved build, so a
  preflight guess is unnecessary — and would wrongly reject the legitimate
  BYO-static-L3 builders of §4.

## Consequences

- **Breaking:** `Hypervisor.build_uplink` and `build_uplink_addr` are removed.
  This is pre-1.0 with in-repo consumers only, so it is a straight cut — no shim,
  no deprecation window (the same cutover discipline as ADR-0013).
- **The build switch is now first-class.** It can be inspected, validated, and
  shaped like any other `Switch`; the `ManagedBuildSwitch` path turns a manual,
  per-backend NAT-bridge recipe into one declared object realized natively per
  driver.
- **The sidecar story is fully uniform.** Build and runtime switches now serve
  DHCP/DNS the same way on every backend, severing the last dependency on any
  backend-native lease/resolver service.
- **Surface grows by one sibling type, not a wider `Switch`.** `ManagedBuildSwitch`
  sits beside `Switch`, mirroring how ADR-0013 kept service flags off the
  topology object — the egress-manufacturing intent is its own type, gated by a
  driver capability, rather than another flag on `Switch`.
- **Touches** (in NET-11, the implementing change): `PLAN.md` §10 (the build-phase
  paragraph), `docs/user/drivers/networking-modes.md`,
  `examples/px_hello.py` (its manual `vmbr9` recipe is superseded by
  `ManagedBuildSwitch`).
