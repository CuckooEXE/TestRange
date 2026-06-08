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
per-driver setup pages (profile shape, storage/uplink prereqs, named uplinks,
`mgmt` semantics, certification status).
[Networking modes](networking-modes.md) covers the `Switch` API and
how each driver realizes the flags (uplink/mgmt/dhcp/dns/nat).
[Out-of-band egress](out-of-band-egress.md) is the per-driver recipe for the
host NAT bridge a named `uplink` points at — TestRange attaches to it but never
builds it ([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)).

## Status

The driver layer is multi-backend (ADR-0008). What ships today:

- **`MockDriver`** — an in-memory reference backend. It needs no hypervisor
  and is the substrate the test suite drives the full orchestration lifecycle
  against. (It simulates the backend, not a real guest, so a live `testrange
  run` of an example to green still needs a real backend.)
- **Proxmox** — a single-node PVE 9.x host (`proxmoxer` over the PVE REST API;
  `pip install -e '.[proxmox]'`): driver primitives are live-proven and wired
  into `pytest -m proxmox` (driver-primitive tests — connect/SDN/storage/VM/QGA);
  the full `tests/plans/` certification sweep is tracked under the REL epic.
  See [Proxmox VE](proxmox.md) for the full setup; multi-node clusters and block
  storage are not yet supported (PVE-31, PVE-33).
  Installer-origin builders (`ProxmoxAnswerBuilder`, `ESXiKickstartBuilder`) also
  need the system `xorriso` binary on the orchestrator host to prepare the
  installer ISO (`apt install xorriso` / `dnf install xorriso` / `brew install
  xorriso`, ADR-0022); it is not needed for cloud-init/image-origin builds.
- **libvirt** — the **certified reference backend** ([ADR-0019](../../adr/0019-libvirt-reference-backend.md)),
  rebuilt against the current multi-backend ABC (BACKEND-1): VM lifecycle, L2 via
  the libvirt network API + sidecar DHCP discovery, the serial build-result sink,
  QGA guest-ops, and per-run directory pools with streamed volume I/O. Exercised
  by the `tests/plans/` corpus + integration suite against a local
  `qemu:///system` (`pip install -e '.[libvirt]'`). See [libvirt](libvirt.md).
- **ESXi (standalone)** — pyVmomi driver, **code-complete and gate-green**
  (`pip install -e '.[esxi]'`): standard vSwitch/portgroup L2, datastore
  `/folder` volume I/O with qcow2↔vmdk conversion at the boundary, VM lifecycle,
  snapshots, VMware Tools guest-ops, and the datastore-file serial sink. `connect`
  + inventory + byte I/O are live-proven; the full `tests/plans/`
  live certification is in progress (ESXI-13). Requires a non-free vSphere
  license (the API write path is license-gated). See [ESXi](esxi.md) and
  [ADR-0025](../../adr/0025-esxi-standalone-driver.md). Hyper-V remains on the
  long-term roadmap.
