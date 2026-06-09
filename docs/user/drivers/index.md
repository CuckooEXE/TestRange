# Driver setup

Each hypervisor backend has its own install + permissions story. Pick
the driver matching your hypervisor and follow that page's prereqs.

```{toctree}
:maxdepth: 1

libvirt
proxmox
esxi
networking-modes
out-of-band-egress
```

[libvirt](libvirt.md), [Proxmox VE](proxmox.md), and [ESXi](esxi.md) are the
per-driver setup pages — each with the same shape: **About**, **Support level**
(where that backend's support/certification tier is stated), **Connection
profile**, and **Egress**, plus backend-specific prerequisites.
[Networking modes](networking-modes.md) covers the `Switch` API and
how each driver realizes the flags (uplink/mgmt/dhcp/dns/nat).
[Out-of-band egress](out-of-band-egress.md) is the per-driver recipe for the
host NAT bridge a named `uplink` points at — TestRange attaches to it but never
builds it ([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)).

## What ships

The driver layer is multi-backend (ADR-0008). Each driver's **Support level**
section (on its own page) is the authority on how far it is validated; this is
just the map of what exists:

- **`MockDriver`** — an in-memory backend used by the unit suite. It needs no
  hypervisor and drives the full orchestration lifecycle in-process. (It
  simulates the backend, not a real guest, so a live `testrange run` of an
  example to green still needs a real backend.)
- **libvirt** ([page](libvirt.md), `pip install -e '.[libvirt]'`) — VM
  lifecycle, L2 via the libvirt network API + sidecar DHCP discovery, the serial
  build-result sink, QGA guest-ops, and per-run directory pools with streamed
  volume I/O, against a local (or `qemu+ssh`) host.
- **Proxmox VE** ([page](proxmox.md), `pip install -e '.[proxmox]'`) — a
  single-node PVE 9.x host over the REST API (`proxmoxer`): SDN switches,
  streamed volume I/O, VM lifecycle, snapshots, QGA exec. Multi-node clusters and
  block storage are not yet supported (PVE-31, PVE-33).
- **ESXi (standalone)** ([page](esxi.md), `pip install -e '.[esxi]'`) — pyVmomi
  driver: standard vSwitch/portgroup L2, datastore `/folder` volume I/O with
  qcow2↔vmdk conversion at the boundary, VM lifecycle, snapshots, VMware Tools
  guest-ops, and the datastore-file serial sink. Requires a non-free vSphere
  license (the API write path is license-gated). See also
  [ADR-0025](../../adr/0025-esxi-standalone-driver.md).

Hyper-V remains on the long-term roadmap. Installer-origin builders
(`ProxmoxAnswerBuilder`, `ESXiKickstartBuilder`) need the system `xorriso` binary
on the orchestrator host to prepare the installer ISO (`apt install xorriso` /
`dnf install xorriso` / `brew install xorriso`, ADR-0022); it is not needed for
cloud-init/image-origin builds.
