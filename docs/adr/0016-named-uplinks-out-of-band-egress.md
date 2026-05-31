# ADR-0016: Uplinks are profile-named; egress is out-of-band; the build switch is portable topology

Status: Accepted
Date: 2026-05-29

**Supersedes [ADR-0014](0014-managed-build-switch.md) in full** — TestRange no
longer manufactures or fences a build-internet egress segment; `ManagedBuildSwitch`,
`ManagedEgress`, and the `supports_managed_build_egress` capability are removed.
**Amends [ADR-0015](0015-backend-binding.md)** — the build switch leaves the
binding and returns to portable topology, and the connection profile gains a
named-uplink map (and a multi-profile, one-file layout). **Amends
[ADR-0008](0008-driver-abc-multi-backend.md) §1** — `Switch.uplink` is a logical
name the driver resolves against a profile-supplied map, a sibling of the
existing `backing_storage` binding knob; the orchestrator still never names a
bridge.

## Context

ADR-0014 had TestRange *manufacture* a build network's internet-egress segment
and *fence* it default-deny — an SDN `snat=1` VNet + VNet firewall on Proxmox,
`New-NetNat` + an Internal vSwitch + Windows firewall on Hyper-V, and *nothing
possible* on ESXi (which has no host-NAT primitive, forcing
`supports_managed_build_egress = False` and a preflight rejection). That asymmetry
was the tell: a "uniform" capability that one of three planned backends
structurally cannot provide is not uniform. The realization (PVE SDN snat + a
VNet-firewall fence, PVE-36/PVE-37 spikes) was also a large, backend-specific
surface for something the host environment already provides.

Two project principles cut against it:

- **You-must-be-this-tall-to-ride** (switch-flags-describe-the-sidecar): the flags
  describe what *TestRange's* sidecar serves, not wire reality. TestRange does not
  police what is on the other side of an uplink — so it should not be in the
  business of *manufacturing* what is on the other side either.
- **No speculative abstraction**: `MagicEgressSwitch` (an earlier cut of this
  proposal) and `ManagedBuildSwitch` are both sugar/intent wrappers around a shape
  that is already spelled by `Switch(uplink=…, sidecar=Sidecar(dhcp,dns,nat))`.

Separately, `Switch.uplink` baked a host-specific NIC name (`"eth0"`, `"vmbr9"`)
into the *portable* plan — the one thing the ADR-0015 binding split set out to
keep out of the committed test. That host specificity was also the sole reason
ADR-0014/ADR-0015 argued the build switch had to live on the binding rather than
on the topology.

## Decision

### 1. Egress is out-of-band; TestRange only attaches to it

A network that routes to the internet is a host bridge the operator provisions
**out-of-band** (a Proxmox `vmbr` with NAT/DHCP behind it, a libvirt NAT network,
an ESXi uplink port-group). TestRange does not create, SNAT, or fence it. A guest
segment reaches it exactly as it always has — a NAT `Sidecar` whose `eth1` rides
that bridge:

```python
Switch("egress", Network("net"), cidr="192.168.2.0/24",
       uplink="nat_egress",                       # a profile-named host bridge
       sidecar=Sidecar(dhcp=True, dns=True, nat=True))
```

The sidecar serves DHCP/DNS to the guests on `switch.cidr` (the guest segment is
isolated, sidecar at `.1`), and MASQUERADEs out `eth1`. `eth1` **DHCPs from the
out-of-band network** by default — no TestRange-assigned static address, because
the subnet is real and serves leases. (NET-7 `Sidecar(addr=StaticAddr(...))`
survives for the case where that network will not lease the sidecar's MAC.) This
is exactly the BYO path ADR-0014 §4 already described; it is now the *only* path.

This is double-NAT for the guest (sidecar → out-of-band NAT → internet). For an
egress-only test range that is harmless and is documented as such.

### 2. No `MagicEgressSwitch`, no `ManagedBuildSwitch` — just `Switch`

Neither type is built. A NAT-egress switch is a plain `Switch` with a NAT
`Sidecar`, as above. There is one spelling, no intent wrapper, no driver
capability gate. `Switch | None` is the build-switch type; `Switch` is the only
network type.

### 3. `uplink` is a profile-named logical name

`Switch.uplink` no longer names a host NIC; it names an entry in the bound
profile's `[uplinks]` map:

```toml
[myProxmox.uplinks]
my_cool_network = "vmbr3"
nat_egress      = "vmbr9"   # the "egress" bridge is just another named uplink
```

Resolution rides on the **driver**, exactly like `backing_storage`: the profile
parses `[uplinks]` and hands the map into `build_driver()`; the driver resolves
`switch.uplink` → host iface inside `create_switch`. An unmapped name is a
`DriverError`, surfaced up-front as a `preflight` finding (`unknown-uplink`) that
names the profile and the missing key. The orchestrator still never names a
bridge — it shuttles the logical name in the `Switch` and the driver does the
lookup. `Switch.__init__` validation is unchanged (non-empty string); it cannot
know the profile.

A profile may map zero uplinks (the empty / localhost-libvirt case). A plan that
references an uplink the bound profile does not map fails at preflight — the
portability contract the binding split promised.

### 4. The build switch is portable topology, on the `Hypervisor`

Because `uplink` is now a logical name, the build switch no longer hard-codes a
host bridge — so it is portable again, and moves back onto the topology:

```python
Hypervisor(
    build_switch=Switch("build", Network("b"), cidr="10.97.99.0/24",
                        uplink="nat_egress", sidecar=Sidecar(dhcp=True, dns=True, nat=True)),
    networks=[...], pools=[...], vms=[...],
)
```

`build_switch: Switch | None` behaves **identically to a run-phase switch** — same
`create_switch`, same uplink resolution, same sidecar. `None` stays the isolated
no-egress build network (DHCP+DNS, no uplink); a build needing apt/pip declares a
`build_switch` with an uplink. This reverses the ADR-0014/ADR-0015/CORE-7
placement (build egress on the binding): that rationale held *only* because the
uplink was host-specific. With named uplinks it is not, so `ResolvedBackend` drops
its `build_switch` field and the profile drops its `[build_switch]` table.
`resolve_build_switch` shrinks to "`None` → isolated default; else honor as
declared."

### 5. The connection profile is one file, many profiles

`--connect PATH` becomes `--profile [<file>:]<name>`, default file `connect.toml`:

- `--profile foobar` → the `[foobar]` profile in `./connect.toml`
- `--profile myOther.toml:foobar` → the `[foobar]` profile in `./myOther.toml`

Each top-level `[name]` table is one profile carrying its own `driver` scheme key,
its backend connection keys, and an optional `[name.uplinks]` sub-table.
`load_profile(path, name)` reads the file, selects the named table, and dispatches
on that table's `driver` key to the registered `BackendProfile` subclass.

## Consequences

- **Breaking (pre-1.0, in-repo consumers only — straight cut, no shim):**
  `ManagedBuildSwitch`, `ManagedEgress`, `supports_managed_build_egress`, the
  `create_switch(managed_egress=…)` kwarg, `preflight.managed_build_egress_findings`,
  the build-egress `[build_switch]` profile table, and `ResolvedBackend.build_switch`
  are all removed. `--connect` is renamed `--profile` with new grammar.
- **Less code, fewer branches.** The PVE SDN snat/VNet-firewall realization
  (`_sdn.py`/`_naming.py`/`driver.py`), the managed-egress addressing
  (`BUILD_EGRESS_CIDR`/`MANAGED_EGRESS_DNS`/`_managed_build_switch`), and the
  capability gate all go away. PVE-36/PVE-37 spikes are dropped.
- **More capable.** A profile can map *many* uplinks; the "egress" one is not
  special. ESXi is no longer a second-class backend — it attaches to an uplink
  port-group like everyone else, with no capability it cannot satisfy.
- **The portable plan is fully host-free again.** `uplink=` is the last
  host-specific value that lived in the committed plan; it is now a logical name,
  resolved by the gitignored profile.
- **Touches:** `networks/base.py` (delete the two types), `connect.py` (multi-
  profile + `[uplinks]`, drop `[build_switch]`), the three profiles, `drivers/base.py`
  + the three drivers (uplink resolution, drop `managed_egress`/capability),
  `orchestrator/{backend,build,build_phase,provision,runtime}.py`, `preflight.py`,
  `hypervisor.py` (`build_switch` field), `cli.py` (`--profile`), `PLAN.md §10`,
  `examples/*` + `connect.toml.example`, and the user/dev docs.
