# Driver setup

Each hypervisor backend has its own install + permissions story. Pick
the driver matching your hypervisor and follow that page's prereqs.

```{toctree}
:maxdepth: 1

networking-modes
out-of-band-egress
```

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
- **Proxmox** — green end-to-end on a single-node PVE 9.x host (`proxmoxer`
  over the PVE REST API; `pip install -e '.[proxmox]'`). See
  `examples/px_hello.py` for a runnable plan and the connection/credential +
  build-egress prereqs. A dedicated setup page is pending (PVE-34); multi-node
  clusters and block storage are not yet supported (PVE-31, PVE-33).
- **libvirt** — deleted and slated for a rebuild against the current ABC; ESXi
  and Hyper-V are on the long-term roadmap. See the `ktui` board and ADR-0008.
