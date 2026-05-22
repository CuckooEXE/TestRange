# Networking modes

`testrange` exposes one Switch API across every driver. Each driver
realizes the flags using its backend's native primitives. This page is
the per-flag reference plus the per-driver mapping table.

## Switch shape (driver-agnostic)

```python
Switch(
    name: str,
    *networks: Network,
    cidr: str = "192.168.10.0/24",   # strict network form; ValueError on host-form
    uplink: str | None = None,        # physical NIC on the hypervisor host
    mgmt: bool = False,               # host adapter at .2 on the segment
    dns: bool = False,                # sidecar serves DNS at .1
    dhcp: bool = False,               # sidecar serves DHCP at .1
    nat: bool = False,                # sidecar MASQUERADEs out the uplink at .1
)
```

The flags are orthogonal except for one rule: **`nat=True` requires
`uplink=`** (the sidecar needs a physical NIC to MASQUERADE out of).
Setting `nat=True` without `uplink=` is a `ValueError` at construction.

## Addressing layout

Every Switch's CIDR carves up the same way, picked up by both the
validator and the sidecar's `dnsmasq` config so the two can never
drift:

| Slot          | Address                | Present when            | Purpose                                                |
|---------------|------------------------|-------------------------|--------------------------------------------------------|
| Sidecar       | `network_address + 1`  | `dhcp \| dns \| nat`    | Gateway when `nat=True`; resolver when `dns=True`      |
| Mgmt          | `network_address + 2`  | `mgmt=True`             | Host adapter on the segment (no NAT, no forwarding)    |
| Reserved      | `.3`–`.9`              | always                  | Future infra; not assignable                           |
| DHCP pool     | `.10`–`.99`            | `dhcp=True`             | Lease range served by the sidecar                      |
| User statics  | `.100`–`.254`          | always                  | Free for `NetworkIface(..., addr=StaticAddr("..."))`        |

Constants live in `testrange/networks/_addressing_consts.py`.

## Per-flag behavior

### `uplink="<nic>"`

The physical NIC on the hypervisor host the Switch attaches to. ESXi
calls this a `vmnic`. The driver — not the user — realizes the L2 segment
and attaches the NIC (ADR-0008 §1). The user never names a pre-existing
bridge/vSwitch.

When `nat=False`, the Switch segment IS the uplink segment: guest frames
egress with their own MACs and IPs. No NAT. Useful for "plug the VM
into the same LAN as the host."

When `nat=True`, the Switch segment stays isolated; the driver realizes a
**second** uplink-facing segment enslaving the physical NIC, and the sidecar
straddles both. See `nat` below for the topology.

### `mgmt=True`

The host gets an L3 interface on the Switch's CIDR at `.2`. It's just
an adapter — no NAT, no forwarding, no router semantics. A VM on the
Switch can `ping 192.168.10.2` and reach the host kernel; the host can
`ping 192.168.10.100` to reach a guest.

A future `Switch(router=True)` is where actual routing semantics will
land. Today `mgmt` is host-on-the-wire only.

### `dhcp=True`

A per-Switch sidecar VM appears at `.1` and serves DHCP leases in
`.10`–`.99` via `dnsmasq`. The sidecar pins the lease file at
`/var/lib/misc/dnsmasq.leases`; the orchestrator reads it back via the
driver's native guest agent (the sidecar bakes in `qemu-guest-agent`) when a
test asks for an IP discovered via DHCP.

Each guest's DHCP lease is keyed on a stable MAC derived from
`(plan_name, vm_name, nic_idx)`, so leases persist across re-creations
of the same VM.

### `dns=True`

The sidecar's `dnsmasq` also resolves `<vmname>.<networkname>` to the
guest's IP — static IPs become `host-record` entries, DHCP-assigned
IPs become `dhcp-host` entries. With `dns=True` *and* `dhcp=True`, the
sidecar advertises itself as the DNS server (DHCP option 6); with
`dhcp=True` and `dns=False`, the DNS listener is disabled (`port=0` in
dnsmasq).

### `nat=True` (requires `uplink=`)

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

Topology with `uplink=eth0, nat=True`:

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

Topology with `uplink=eth0, nat=False`:

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

Each driver realizes the same `Switch` flags with its backend's native L2
primitives (ADR-0008 §1: the driver owns the Switch; the orchestrator never
names a bridge). The sidecar-served `dhcp`/`dns`/`nat` story is uniform — one
Alpine image, one config-ISO contract, no per-driver branching. Only the L2
realization (`create_switch`/`create_network`) is driver-specific.

| Flag       | MockDriver (reference)        | Proxmox (in progress)         | ESXi / Hyper-V (future)            |
|------------|-------------------------------|-------------------------------|------------------------------------|
| `uplink`   | Simulated segment record      | Create SDN zone + vnet, attach physical | vSwitch + vmnic / external vSwitch |
| `mgmt`     | Simulated `.2` adapter        | Bridge IP via SDN             | vmkernel adapter / share with mgmt OS |
| `dhcp/dns` | Sidecar VM model              | Same                          | Same                               |
| `nat`      | Sidecar VM model              | Same                          | Same                               |

**General limits** (driver-agnostic):

- Host-local L2 realization (e.g. netlink bridge management) is local-only;
  a Switch with `uplink`/`nat`/`mgmt` over a remote backend connection is
  caught by preflight (`remote_uplink_unsupported`).
- The sidecar's `eth1` DHCPs from the upstream LAN — if the LAN doesn't lease
  (MAC whitelist, isolated VLAN), NAT silently breaks.
- One Switch is one CIDR. If you need two subnets, declare two Switches.

**Sidecar build** (needed once for any `dhcp`/`dns`/`nat` Switch):

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
- Static IP can't collide with `.1` (sidecar) when `needs_sidecar`, or
  `.2` (mgmt) when `mgmt=True`.
- Static IP can't fall in `.10`–`.99` when `dhcp=True`.
- A NIC with no address (`addr=None`) is allowed on any Switch,
  including `dhcp=False`: it renders unconfigured (`dhcp4: false`) and
  the guest OS decides what to do. There is no static address to
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
       mgmt=True, dhcp=True, dns=True)

# Full internet — guests DHCP, resolve, and NAT out the uplink.
Switch("internet", Network("a"), cidr="10.53.0.0/24",
       uplink="eth0", mgmt=True, dhcp=True, dns=True, nat=True)

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
(`uplink=build_uplink, dhcp=True, dns=True, nat=True`, CIDR
`10.97.99.0/24`), brings up the sidecar, runs the build VMs, then
tears it all down LIFO. Skip `build_uplink=` only if every VM
already has a cache hit (i.e. the full `_built_<config_hash>__*` disk
set is already in the cache).
