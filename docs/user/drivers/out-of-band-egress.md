# Out-of-band egress (the "magic" NAT bridge)

A `Switch` that reaches the internet does so through an **uplink** — a logical
name (`Switch(uplink="egress")`) the bound profile's `[uplinks]` map resolves to
a host bridge. TestRange **attaches** to that bridge; it does **not** create,
NAT, route, or firewall it ([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)).
The bridge — with NAT/DHCP/route to the internet behind it — is something *you*
provision once on the hypervisor host, **out of band**. This page is the
per-driver recipe for that bridge, plus why it works this way.

## What "egress" actually has to provide

For a NAT switch — `Switch(uplink="egress", sidecar=Sidecar(dhcp=True, dns=True, nat=True))` —
the topology is two segments (see [Networking modes](networking-modes.md)):

- the **guest segment** (isolated, `switch.cidr`): the sidecar serves DHCP/DNS
  here and is the guests' gateway;
- the **uplink segment**: the sidecar's `eth1` is attached to the bridge your
  `egress` name resolves to, and the sidecar MASQUERADEs guest traffic out of it.

So the egress bridge must give the sidecar's `eth1` two things:

1. **an address on it** — by default `eth1` **DHCPs** from the bridge's network.
   If nothing serves DHCP there, pin it statically in the plan instead:
   `Sidecar(..., addr=StaticAddr("10.255.255.2/24", gw="10.255.255.1", dns=("1.1.1.1",)))`
   (NET-7);
2. **a route to the internet** — the bridge is either bridged to a LAN that
   routes out, or the host NATs (`MASQUERADE`) it out a real NIC.

That's it. Anything that satisfies those two is a valid `egress`. The result is
double-NAT for the guest (guest → sidecar → out-of-band NAT → internet); for an
egress-only test range that is harmless.

## Why you'd want it

- **The build phase needs the internet.** `apt` / `pip` pull packages while a
  build VM boots; without an egress-capable `build_switch` the build network is
  isolated and a package install fails (see [writing a plan](../writing-a-plan.md)).
- **Run-phase VMs that must reach the internet** (a guest that `curl`s an
  external service, resolves public DNS, downloads at test time).
- **A single-public-IP lab host**, where every guest has to share one egress
  through host NAT rather than getting its own LAN address.

If none of your VMs need the internet (fully air-gapped topology, or every disk
is already a cache hit), you need no egress bridge at all — omit `uplink` and
`build_switch`.

## Why TestRange doesn't build it for you

An earlier design (`ManagedBuildSwitch`, ADR-0014) had TestRange *manufacture and
fence* this segment — an SDN `snat=1` vnet + VNet firewall on Proxmox, a NAT
network + `nwfilter` on libvirt, `New-NetNat` + Windows firewall on Hyper-V. It
was removed in [ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md) for
four reasons:

1. **It wasn't uniform.** ESXi has no host-NAT primitive, so the "capability" was
   structurally impossible on a backend we plan to support — a uniform feature
   one backend can't provide isn't uniform.
2. **Large backend-specific surface for something the host already does.** Each
   backend reimplemented a NAT-bridge-plus-firewall recipe that the host OS does
   in a few lines of config.
3. **"You must be this tall to ride."** TestRange's switch/sidecar flags describe
   what *its own* sidecar serves, not wire reality — and it deliberately does not
   police or own out-of-band services on a segment. Manufacturing the egress
   contradicted that boundary; attaching to an operator-owned bridge respects it.
4. **It kept host specifics out of the portable plan the wrong way.** Named
   uplinks solve that cleanly: the plan says `uplink="egress"`, and the gitignored
   profile maps the name to a host bridge. The host's NAT story stays on the host.

The upshot: provisioning egress is a **one-time host setup**, documented below,
not a TestRange runtime concern.

## Proxmox

`egress` must resolve to an **existing Linux bridge** on the PVE node (preflight
verifies it exists). The sidecar's `eth1` becomes a NIC on that bridge.

### Option A — reuse a bridge that already routes out

If `vmbr0` is bridged to a LAN that serves DHCP and routes to the internet (the
common single-node setup), you need no new bridge. Map the name:

```toml
[pve.uplinks]
egress = "vmbr0"
```

The sidecar's `eth1` DHCPs from the LAN and NATs guests out through it.

### Option B — a dedicated NAT bridge (isolated host, one public IP)

When the node's only route out is its management NIC and you don't want guests on
the management LAN, make a port-less bridge and have the host masquerade it.
Add to `/etc/network/interfaces` (then `ifreload -a`):

```text
auto vmbr9
iface vmbr9 inet static
    address 10.255.255.1/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up   sysctl -w net.ipv4.ip_forward=1
    post-up   iptables -t nat -A POSTROUTING -s 10.255.255.0/24 -o vmbr0 -j MASQUERADE
    post-down iptables -t nat -D POSTROUTING -s 10.255.255.0/24 -o vmbr0 -j MASQUERADE
```

```toml
[pve.uplinks]
egress = "vmbr9"
```

`vmbr9` has no DHCP server, so give the sidecar's `eth1` a static address on it
in the plan (NET-7):

```python
Switch(
    "egress-sw", Network("net"), cidr="192.168.2.0/24", uplink="egress",
    sidecar=Sidecar(
        dhcp=True, dns=True, nat=True,
        addr=StaticAddr("10.255.255.2/24", gw="10.255.255.1", dns=("1.1.1.1",)),
    ),
)
```

(Or run `dnsmasq` on `vmbr9` and drop the `addr=` so `eth1` DHCPs instead.)

```{note}
The Proxmox driver is **proxmoxer-only** for the control plane and never touches
host networking. This bridge is host config you create once by hand — TestRange
only attaches the sidecar's `eth1` to it. If you prefer PVE's own SDN, a
simple-zone vnet with `snat=1` works too, but that is exactly the "manufacture"
step ADR-0016 deliberately leaves to you.
```

## libvirt

`egress` resolves to a host bridge the libvirt driver attaches the sidecar's
`eth1` to.

### Option A — the built-in `default` network (`virbr0`)

libvirt ships a `default` NAT network: bridge `virbr0`, subnet
`192.168.122.0/24`, with `dnsmasq` serving DHCP and `forward mode='nat'` already
masquerading out the host's real NIC. Make sure it's running and map it:

```sh
virsh net-start default        # if not already active
virsh net-autostart default
```

```toml
[libvirt-local.uplinks]
egress = "virbr0"
```

The sidecar's `eth1` DHCPs from `192.168.122.0/24` and NATs out — zero extra host
setup.

### Option B — a dedicated NAT network

To keep test egress off the `default` network, define your own. Save as
`tr-egress.xml`:

```xml
<network>
  <name>tr-egress</name>
  <forward mode='nat'/>
  <bridge name='virbr9'/>
  <ip address='10.255.255.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='10.255.255.10' end='10.255.255.99'/>
    </dhcp>
  </ip>
</network>
```

```sh
virsh net-define tr-egress.xml
virsh net-start tr-egress
virsh net-autostart tr-egress
```

```toml
[libvirt-local.uplinks]
egress = "virbr9"
```

`forward mode='nat'` gives the route out and `<dhcp>` leases the sidecar's `eth1`,
so no static `addr=` is needed.

```{note}
The libvirt driver's L2 realization (attaching the sidecar's `eth1` to the named
bridge) lands with **BACKEND-1.2**; the recipes above are the intended shape.
Host-local bridge management is **local-only** — a named uplink over a remote
`qemu+ssh://` connection is a separate piece of work (BACKEND-5) and is caught at
preflight (`remote_uplink_unsupported`) until then.
```

## See also

- [Networking modes](networking-modes.md) — the full `Switch`/`Sidecar` API and
  the two-segment NAT topology.
- [Connecting to a backend](../connecting-to-a-backend.md) — the `[uplinks]` map
  and the `--profile` workflow.
- [ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md) — the decision
  to make uplinks profile-named and leave egress out-of-band.
