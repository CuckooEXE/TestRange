# Networking modes

`testrange` exposes one Switch API across every driver. A Switch owns the
**L2 topology** (`cidr`, `uplink`, `mgmt`); the **services** a sidecar VM
serves at `.1` (`dhcp`, `dns`, `nat`) are bundled into an optional `Sidecar`
the Switch carries. Each driver realizes the topology using its backend's
native primitives; the sidecar story is uniform across backends. This page is
the per-knob reference plus the per-driver mapping table.

## Switch shape (driver-agnostic)

```python
Sidecar(
    dhcp: bool = False,               # sidecar serves DHCP at .1
    dns:  bool = False,               # sidecar serves DNS at .1
    nat:  bool = False,               # sidecar MASQUERADEs out the uplink at .1
    addr: StaticAddr | None = None,   # static sidecar eth1 (NET-7); else DHCP-from-LAN
)

Switch(
    name: str,
    *networks: Network,
    cidr: str = "192.168.10.0/24",   # strict network form; ValueError on host-form
    uplink: str | None = None,        # physical NIC on the hypervisor host
    mgmt: bool = False,               # host adapter at .2 on the segment
    sidecar: Sidecar | None = None,   # services at .1, or None for a bare wire
)
```

`sidecar=None` is a bare switch — a pure L2 wire with no services. There is no
all-off `Sidecar`: one that serves nothing is a `ValueError` (use `sidecar=None`).

Two validation rules to know:

- **`Sidecar(nat=True)` requires `uplink=`** on the Switch (the sidecar needs a
  physical NIC to MASQUERADE out of). A NAT sidecar on an uplink-less Switch is
  a `ValueError` at construction — and `Switch` is where it's caught, because it
  is the only object that sees both the uplink and the services.
- **`Sidecar(addr=...)` requires `nat=True`** and an explicit prefix (the
  sidecar's `eth1` lives on the uplink's own subnet, not the Switch CIDR). Both
  are `ValueError`s from `Sidecar` itself.

## Addressing layout

Every Switch's CIDR carves up the same way, picked up by both the
validator and the sidecar's `dnsmasq` config so the two can never
drift:

| Slot          | Address                | Present when               | Purpose                                                |
|---------------|------------------------|----------------------------|--------------------------------------------------------|
| Sidecar       | `network_address + 1`  | `sidecar is not None`      | Gateway when the sidecar has `nat`; resolver when `dns`|
| Mgmt          | `network_address + 2`  | `mgmt=True`                | Host adapter on the segment (no NAT, no forwarding)    |
| Reserved      | `.3`–`.9`              | always                     | Future infra; not assignable                           |
| DHCP pool     | `.10`–`.99`            | sidecar has `dhcp`         | Lease range served by the sidecar                      |
| User statics  | `.100`–`.254`          | always                     | Free for `NetworkIface(..., addr=StaticAddr("..."))`        |

Constants live in `testrange/networks/_addressing_consts.py`.

## Switch knobs (L2 topology)

### `uplink="<nic>"`

The physical NIC on the hypervisor host the Switch attaches to. ESXi
calls this a `vmnic`. The driver — not the user — realizes the L2 segment
and attaches the NIC (ADR-0008 §1). The user never names a pre-existing
bridge/vSwitch.

Without a NAT sidecar, the Switch segment IS the uplink segment: guest frames
egress with their own MACs and IPs. No NAT. Useful for "plug the VM into the
same LAN as the host" — and note this works with `sidecar=None`, which is
exactly why `uplink` is a Switch knob and not a sidecar service.

With a `Sidecar(nat=True)`, the Switch segment stays isolated; the driver
realizes a **second** uplink-facing segment enslaving the physical NIC, and the
sidecar straddles both. See `nat` below for the topology.

### `mgmt=True`

The host gets an L3 interface on the Switch's CIDR at `.2`. It's just
an adapter — no NAT, no forwarding, no router semantics. A VM on the
Switch can `ping 192.168.10.2` and reach the host kernel; the host can
`ping 192.168.10.100` to reach a guest.

A future `Switch(router=True)` is where actual routing semantics will
land. Today `mgmt` is host-on-the-wire only.

## Sidecar services

Pass these inside `sidecar=Sidecar(...)`. Any non-`None` sidecar materializes
one per-Switch VM at `.1`; the fields select which services it runs.

### `Sidecar(dhcp=True)`

The sidecar at `.1` serves DHCP leases in `.10`–`.99` via `dnsmasq`. The
sidecar pins the lease file at `/var/lib/misc/dnsmasq.leases`; the orchestrator
reads it back via the driver's native guest agent (the sidecar bakes in
`qemu-guest-agent`) when a test asks for an IP discovered via DHCP.

Each guest's DHCP lease is keyed on a stable MAC derived from
`(plan_name, vm_name, nic_idx)`, so leases persist across re-creations
of the same VM.

### `Sidecar(dns=True)`

The sidecar's `dnsmasq` also resolves `<vmname>.<networkname>` to the
guest's IP — static IPs become `host-record` entries, DHCP-assigned
IPs become `dhcp-host` entries. With `dns=True` *and* `dhcp=True`, the
sidecar advertises itself as the DNS server (DHCP option 6); with
`dhcp=True` and `dns=False`, the DNS listener is disabled (`port=0` in
dnsmasq).

### `Sidecar(nat=True)` (requires `Switch(uplink=...)`)

The sidecar MASQUERADEs guest traffic out the uplink. Implementation:

- The driver realizes two L2 segments: an isolated **switch
  segment** (guests + sidecar `eth0` at `.1`) and a separate **uplink
  segment** enslaving the physical NIC (sidecar `eth1`, DHCP-from-LAN).
- The sidecar's `/etc/nftables.nft` defines one POSTROUTING chain with
  `oifname "eth1" masquerade`.
- `net.ipv4.ip_forward=1` is set via `/etc/sysctl.d/99-testrange.conf`.
- DHCP option 3 (router) is advertised as `.1` (the sidecar). With
  `dhcp=True` guests pick it up automatically; with static-IP guests
  the orchestrator bakes `gateway=.1` into the cloud-init netplan.

By default the sidecar's `eth1` DHCPs an address from the upstream LAN. Set
`Sidecar(addr=StaticAddr("10.10.10.2/24", gw=..., dns=[...]))` (NET-7) to pin it
to a static address instead — for hosts that won't lease the sidecar's MAC
(single-public-IP boxes where `uplink` is an internal bridge the host itself
NATs). The address needs an explicit prefix (the uplink is its own subnet, not
the Switch CIDR), and with a static `eth1` the sidecar's `dnsmasq` is pointed at
`addr.dns` explicitly (it can't read a DHCP-populated `resolv.conf`).

Topology with `uplink="eth0", sidecar=Sidecar(nat=True)`:

```
  Guests (.100-.254)
    │
    ▼
  ┌──────────────────────────────────────┐
  │ switch segment (isolated)            │
  │ ──── host .2 (if mgmt=True) ─────    │
  └───────────────────┬──────────────────┘
                      │
              sidecar eth0 (.1, dnsmasq, gateway)
                      │
                  IP forwarding + nftables MASQUERADE
                      │
              sidecar eth1 (DHCP from upstream LAN)
                      │
  ┌──────────────────────────────────────┐
  │ uplink segment (enslaves eth0)       │
  └───────────────────┬──────────────────┘
                      │
                    eth0 → physical LAN
```

Topology with `uplink="eth0", sidecar=None`:

```
  Guests (their own MACs/IPs)
    │
    ▼
  ┌──────────────────────────────────────┐
  │ switch segment (enslaves eth0)       │
  │ ──── host .2 (if mgmt=True) ─────    │
  └───────────────────┬──────────────────┘
                      │
                    eth0 → physical LAN
```

## Per-driver mapping

Each driver realizes the same Switch topology with its backend's native L2
primitives (ADR-0008 §1: the driver owns the Switch; the orchestrator never
names a bridge). The sidecar-served `dhcp`/`dns`/`nat` story is uniform — one
Alpine image, one config-ISO contract, no per-driver branching. Only the L2
realization (`create_switch`/`create_network`) is driver-specific.

| Knob        | MockDriver (reference)        | Proxmox (single-node)         | ESXi / Hyper-V (future)            |
|-------------|-------------------------------|-------------------------------|------------------------------------|
| `uplink`    | Simulated segment record      | Create SDN zone + vnet, attach physical | vSwitch + vmnic / external vSwitch |
| `mgmt`      | Simulated `.2` adapter        | Bridge IP via SDN             | vmkernel adapter / share with mgmt OS |
| `Sidecar`   | Sidecar VM model              | Same                          | Same                               |

**General limits** (driver-agnostic):

- Host-local L2 realization (e.g. netlink bridge management) is local-only;
  a Switch with `uplink`/`mgmt` or a NAT sidecar over a remote backend
  connection is caught by preflight (`remote_uplink_unsupported`).
- The sidecar's `eth1` DHCPs from the upstream LAN — if the LAN doesn't lease
  (MAC whitelist, isolated VLAN), NAT silently breaks. Pin it with
  `Sidecar(addr=...)` (NET-7) when that's the case.
- One Switch is one CIDR. If you need two subnets, declare two Switches.

**Sidecar build** (needed once for any Switch carrying a `Sidecar`):

```sh
sudo ./tools/build-sidecar-image/build.sh
testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 \
    --name testrange-sidecar
```

## Plan-level rules (driver-agnostic)

The validator applied at Hypervisor construction (`MockHypervisor` today,
and any future hypervisor) enforces:

- Static IP must be inside the owning Switch's CIDR.
- Static IP can't equal network/broadcast.
- Static IP can't collide with `.1` (sidecar) when the Switch has a sidecar,
  or `.2` (mgmt) when `mgmt=True`.
- Static IP can't fall in `.10`–`.99` when the sidecar serves `dhcp`.
- A NIC with no address (`addr=None`) is allowed on any Switch,
  including one with no DHCP sidecar: it renders unconfigured (`dhcp4: false`)
  and the guest OS decides what to do. There is no static address to
  range-check, so plan-time validation skips it.
- Duplicate static IPs within the same Network across VMs are rejected.

Every problem is collected and reported in one `ValueError` — fix
once, retry, see the next one.

## Examples by mode

```python
# Bare L2 switch — guests can talk to each other on .100-.254 statics
# but get no address otherwise (set addr=StaticAddr(...) on each NIC).
Switch("isolated", Network("a"), cidr="10.50.0.0/24")

# Mgmt-only — host reachable at .2; static guests at .100-.254.
Switch("mgmt-only", Network("a"), cidr="10.51.0.0/24", mgmt=True)

# DHCP + DNS, no internet — guests get leases and resolve each other,
# but cannot reach upstream.
Switch("intranet", Network("a"), cidr="10.52.0.0/24",
       mgmt=True, sidecar=Sidecar(dhcp=True, dns=True))

# Full internet — guests DHCP, resolve, and NAT out the uplink.
Switch("internet", Network("a"), cidr="10.53.0.0/24",
       uplink="eth0", mgmt=True, sidecar=Sidecar(dhcp=True, dns=True, nat=True))

# Pure bridged — guests join the host's LAN with their own MACs/IPs.
# Upstream DHCP gives them addresses; no testrange sidecar runs.
Switch("lan", Network("a"), cidr="192.168.1.0/24", uplink="eth0")
```

The shipped `examples/network_modes.py` exercises four of these in one
plan (`bare-sw`, `mgmt-sw`, `uplink-sw`, `both-sw`).

## Where to set `build_uplink`

The build phase needs internet for `apt` / `pip`. Set the physical
uplink on the hypervisor:

```python
MockHypervisor(
    build_uplink="eth0",
    networks=[...],
    ...
)
```

The orchestrator synthesizes a transient build Switch
(`uplink=build_uplink, sidecar=Sidecar(dhcp=True, dns=True, nat=True)`, CIDR
`10.97.99.0/24`), brings up the sidecar, runs the build VMs, then
tears it all down LIFO. Skip `build_uplink=` only if every VM
already has a cache hit (i.e. the full `_built_<config_hash>__*` disk
set is already in the cache).
