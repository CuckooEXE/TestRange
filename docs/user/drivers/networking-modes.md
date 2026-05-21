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
| User statics  | `.100`–`.254`          | always                  | Free for `LibvirtNetworkIface(..., addr=StaticAddr("..."))` |

Constants live in `testrange/networks/_addressing_consts.py`.

## Per-flag behavior

### `uplink="<nic>"`

The physical NIC on the hypervisor host the Switch is bridged to. ESXi
calls this a `vmnic`. testrange — not the user — creates the bridge
and attaches the NIC. The user never names a pre-existing bridge.

When `nat=False`, the Switch bridge IS the uplink bridge: guest frames
egress with their own MACs and IPs. No NAT. Useful for "plug the VM
into the same LAN as the host."

When `nat=True`, the Switch bridge stays isolated; testrange creates a
**second** bridge enslaving the physical NIC, and the sidecar straddles
both. See `nat` below for the topology.

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
QEMU Guest Agent (the sidecar bakes in `qemu-guest-agent`) when a
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

- testrange creates two bridges via pyroute2: an isolated **switch
  bridge** (guests + sidecar `eth0` at `.1`) and a separate **uplink
  bridge** enslaving the physical NIC (sidecar `eth1`, DHCP-from-LAN).
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
  ┌─────────────────────────────────────┐
  │ switch bridge: tr-<hash> (isolated) │
  │ ──── host .2 (if mgmt=True) ─────   │
  └───────────────────┬─────────────────┘
                      │
              sidecar eth0 (.1, dnsmasq, gateway)
                      │
                  IP forwarding + nftables MASQUERADE
                      │
              sidecar eth1 (DHCP from upstream LAN)
                      │
  ┌─────────────────────────────────────┐
  │ uplink bridge: tr-<hash> (enslaves eth0) │
  └───────────────────┬─────────────────┘
                      │
                    eth0 → physical LAN
```

Topology with `uplink=eth0, nat=False`:

```
  Guests (their own MACs/IPs)
    │
    ▼
  ┌──────────────────────────────────────────┐
  │ switch bridge: tr-<hash> (enslaves eth0) │
  │ ──── host .2 (if mgmt=True) ─────        │
  └───────────────────┬──────────────────────┘
                      │
                    eth0 → physical LAN
```

## Per-driver mapping

### libvirt (`LibvirtDriver`)

| Flag       | Realized via                                                                                                                                          |
|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------|
| `uplink`   | pyroute2 creates a bridge (`tr-<hash>`) and enslaves the NIC. libvirt network XML uses `<forward mode='bridge'/><bridge name='tr-...'/>`.             |
| `mgmt`     | pyroute2 assigns `.2/<prefix>` to the bridge. No libvirt `<ip>` element — even on isolated mgmt switches we create our own bridge for a uniform path. |
| `dhcp`     | Sidecar VM runs `dnsmasq` on `eth0`; orchestrator renders one `dhcp-range` per Switch.                                                                |
| `dns`      | Same sidecar `dnsmasq` (or a separate listener if `dhcp=False`); `host-record` per static NIC.                                                         |
| `nat`      | Two-bridge topology + sidecar's `nftables` MASQUERADE on `eth1` + `ip_forward=1`.                                                                     |

**Requirements**: `pyroute2`, `nftables` (in the sidecar image),
`libvirt-python`, and `CAP_NET_ADMIN` for bridge creation (typically
root, or a granted capability).

**Limits**:

- `pyroute2` is local-netlink only. Any Switch with `uplink`, `nat`,
  or `mgmt` plus a remote libvirt URI (`qemu+ssh://...`) fails
  preflight with `remote_uplink_unsupported`. Bare or
  sidecar-only Switches on remote URIs are fine.
- The sidecar's `eth1` DHCPs from the upstream LAN — if the LAN
  doesn't lease (MAC whitelist, isolated VLAN), NAT silently breaks.
  Future preflight hook can verify the lease via QGA.
- One Switch is one CIDR. If you need two subnets, declare two
  Switches.

**Sidecar build**:

```sh
sudo ./tools/build-sidecar-image/build.sh
testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 \
    --name testrange-sidecar
```

### ESXi / Proxmox / Hyper-V (future)

The same `Switch` API will translate as:

| Flag       | ESXi                                 | Proxmox                       | Hyper-V                            |
|------------|--------------------------------------|-------------------------------|------------------------------------|
| `uplink`   | Create vSwitch + attach vmnic        | Create Zone + attach physical | Create vSwitch (external) on NIC   |
| `mgmt`     | Add vmkernel adapter on the vSwitch  | Bridge IP via SDN             | Allow management OS to share NIC   |
| `dhcp/dns` | Same sidecar VM model                | Same                          | Same                               |
| `nat`      | Same sidecar VM model                | Same                          | Same                               |

Sidecar-served `dhcp`/`dns`/`nat` is uniform across drivers by design:
one Alpine image, one config-ISO contract, no per-driver branching for
the DHCP/DNS/NAT story. Only the bridge-creation primitive is
driver-specific.

## Plan-level rules (driver-agnostic)

The validator applied at `LibvirtHypervisor` construction (and at any
future hypervisor's construction) enforces:

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

## Where to set `install_uplink`

The install phase needs internet for `apt` / `pip`. Set the physical
uplink on the hypervisor:

```python
LibvirtHypervisor(
    connection="qemu:///system",
    install_uplink="eth0",
    networks=[...],
    ...
)
```

The orchestrator synthesizes a transient `Switch("__install", ...,
uplink=install_uplink, dhcp=True, dns=True, nat=True)` (CIDR
`10.97.99.0/24`), brings up the sidecar, runs the install VMs, then
tears it all down LIFO. Skip `install_uplink=` only if every VM
already has a cache hit (i.e. `_post_install_<config_hash>` is already
in the cache).
