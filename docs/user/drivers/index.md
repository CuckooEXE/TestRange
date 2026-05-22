# Driver setup

Each hypervisor backend has its own install + permissions story. Pick
the driver matching your hypervisor and follow that page's prereqs.

```{toctree}
:maxdepth: 1

networking-modes
```

[Networking modes](networking-modes.md) covers the `Switch` API and
how each driver realizes the flags (uplink/mgmt/dhcp/dns/nat).

## Status

The driver layer is multi-backend (ADR-0008). What ships today:

- **`MockDriver`** — an in-memory reference backend. It needs no hypervisor
  and is the substrate the test suite drives the full orchestration lifecycle
  against. (It simulates the backend, not a real guest, so a live `testrange
  run` of an example to green still needs a real backend.)
- **Proxmox** — in progress (`proxmoxer` over the PVE REST API). When it lands
  it gets its own setup page here, with the `pip install -e '.[proxmox]'`
  extra and the connection/credential prereqs.
- **libvirt** — deleted and slated for a rebuild against the current ABC; ESXi
  and Hyper-V are on the long-term roadmap. See `TODO.md` and ADR-0008.
