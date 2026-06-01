# Driver setup

Each hypervisor backend has its own install + permissions story. Pick
the driver matching your hypervisor and follow that page's prereqs.

```{toctree}
:maxdepth: 1

proxmox
networking-modes
out-of-band-egress
```

[Proxmox VE](proxmox.md) is the per-driver setup page (profile shape, storage
prereqs, named uplinks, `mgmt` semantics, certification status).
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
- **Proxmox** — **certified** on a single-node PVE 9.x host (`proxmoxer`
  over the PVE REST API; `pip install -e '.[proxmox]'`): the portable
  `examples/capabilities.py` runs full-green, wired into `pytest -m proxmox`.
  See [Proxmox VE](proxmox.md) for the full setup; multi-node clusters and block
  storage are not yet supported (PVE-31, PVE-33).
  Installer-origin builders (`ProxmoxAnswerBuilder`, `ESXiKickstartBuilder`) also
  need the system `xorriso` binary on the orchestrator host to prepare the
  installer ISO (`apt install xorriso` / `dnf install xorriso` / `brew install
  xorriso`, ADR-0022); it is not needed for cloud-init/image-origin builds.
- **libvirt** — rebuilt against the current multi-backend ABC (BACKEND-1):
  VM lifecycle, L2 via the libvirt network API + sidecar DHCP discovery, the
  serial build-result sink, QGA guest-ops, and per-run directory pools with
  streamed volume I/O. Exercised by the capabilities + integration suite against
  a local `qemu:///system` (`pip install -e '.[libvirt]'`); the rebuild is still
  wrapping up. ESXi and Hyper-V are on the long-term roadmap. See the `ktui`
  board and ADR-0008.
