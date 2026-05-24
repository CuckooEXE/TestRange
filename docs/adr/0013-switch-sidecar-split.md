# ADR-0013: A Switch owns L2 topology; a Sidecar bundles the services it runs

Status: Accepted
Date: 2026-05-24

Supersedes the flat-flag Switch surface in [ADR-0009](0009-mgmt-switch-semantics.md)
(the `dhcp`/`dns`/`nat` knobs move off `Switch`). Relates to
[ADR-0008](0008-driver-abc-multi-backend.md) (the driver realizes the Switch's
L2; the sidecar story stays uniform across backends).

## Context

The original `Switch` carried every networking decision as a flat keyword soup:

```python
Switch(name, *networks, cidr=..., uplink=..., mgmt=...,
       dns=..., dhcp=..., nat=..., uplink_addr=...)
```

Two unrelated concerns lived side by side on that surface:

- **L2 topology** — `cidr`, `uplink`, `mgmt`. What the wire *is*, and which
  host/physical NICs touch it. The driver realizes this (ADR-0008 §1).
- **Sidecar services** — `dhcp`, `dns`, `nat`, plus `uplink_addr` (the static
  IP for the sidecar's MASQUERADE NIC). What *TestRange's* sidecar VM serves at
  `.1`. Uniform across backends (one Alpine image, one config-ISO contract).

Mixing them invited four problems:

- **No type telling you a sidecar is even involved.** `needs_sidecar` was a
  derived `dhcp or dns or nat`; the services weren't a thing you could pass
  around, validate as a unit, or reason about.
- **`uplink_addr` only ever made sense with `nat`** — but a flat field can't
  express "these four only travel together," so the invariant was a runtime
  check buried in `Switch.__init__` next to unrelated ones.
- **The pure-bridge mode** (`uplink=` with **no** sidecar — guests join the
  host LAN with their own MACs) proves `uplink` is *topology*, not a sidecar
  service: an uplinked Switch with no DHCP/DNS/NAT is a legitimate, common
  shape. So `uplink` cannot live "with the services."
- The surface grew monotonically; every future service flag widened `Switch`.

## Decision

**Split the surface along the concern boundary. `Switch` keeps L2 topology;
a new frozen `Sidecar` value object bundles the services, and `Switch` carries
at most one (`sidecar: Sidecar | None`).**

```python
@dataclass(frozen=True)
class Sidecar:
    dhcp: bool = False
    dns:  bool = False
    nat:  bool = False
    addr: StaticAddr | None = None   # was Switch.uplink_addr (NET-7)

Switch(
    name, *networks,
    cidr: str = "192.168.10.0/24",   # L2: strict network form; host-form raises
    uplink: str | None = None,        # L2: physical NIC (incl. the pure-bridge mode)
    mgmt: bool = False,               # L2: host adapter at .2
    sidecar: Sidecar | None = None,   # services run at .1, or None for a bare wire
)
```

### 1. `sidecar=None` is the bare switch; an empty `Sidecar` is forbidden

`needs_sidecar` becomes exactly `sidecar is not None`. There is one way to ask
for "no services": `sidecar=None`. An all-off `Sidecar()` serves nothing, so
`Sidecar.__post_init__` rejects it — there is no second, redundant spelling of
"bare". This keeps the "is there a sidecar VM?" question a single `is None`
check at every consumer (provision, validate, the drivers, the renderers).

### 2. Validation splits along the same boundary

- **Intrinsic to the services** → `Sidecar.__post_init__`: at-least-one-service;
  `addr` requires `nat=True`; `addr` needs an explicit prefix (the uplink is its
  own subnet, not the Switch CIDR, so the netmask can't be derived).
- **Spanning topology + services** → `Switch.__init__`: `nat` requires `uplink`.
  This is the *one* invariant touching both halves, and `Switch` is the only
  object that sees both — so it is the only correct place for it. The sidecar
  alone cannot know whether an uplink exists, so a `Sidecar(nat=True)` is
  well-formed in isolation; it is the Switch that rejects it without an uplink.

### 3. The driver/orchestrator boundary is unchanged

`Hypervisor.build_uplink` / `build_uplink_addr` stay as-is; the build phase
synthesizes the `Sidecar` internally (`Sidecar(dhcp, dns, nat, addr=...)`).
The sidecar renderers (`testrange/networks/sidecar.py`) read services through
`switch.sidecar` and assert the caller honored the `needs_sidecar` contract
(they are only invoked for a switch that has one). Drivers read NAT as
`switch.sidecar is not None and switch.sidecar.nat`; `switch.uplink` reads are
untouched — it never moved.

## Consequences

- The surface now reads as two concerns. Adding a future service (a resolver
  knob, a captive portal) widens `Sidecar`, not `Switch`; adding an L2 feature
  (VLAN tag, second uplink) widens `Switch`, not the services.
- Hard, pre-1.0 cutover: every `Switch(dhcp=..., dns=..., nat=..., uplink_addr=...)`
  call site moved to `sidecar=Sidecar(...)` in one breaking change (all examples
  and the unit suite migrated together; no shim, no deprecation window).
- `addr`'s "only with NAT" and "needs a prefix" rules now live on the object
  they describe, so a `Sidecar` is self-validating wherever it is constructed —
  including the build phase, which previously relied on `Switch` to police them.
- The `mgmt` semantics still await [ADR-0009](0009-mgmt-switch-semantics.md);
  this ADR only relocates the service flags, it does not change what `mgmt`
  means or unblock it.
