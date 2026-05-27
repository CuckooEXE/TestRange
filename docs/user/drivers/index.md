# Driver setup

Each hypervisor backend has its own install + permissions story. Pick
the driver matching your hypervisor and follow that page's prereqs.

```{toctree}
:maxdepth: 1

libvirt
networking-modes
```

[Networking modes](networking-modes.md) covers the `Switch` API and
how each driver realizes the flags (uplink/mgmt/dhcp/dns/nat).

## Roadmap

Only libvirt + KVM is shipped today. Proxmox / ESXi / Hyper-V are on the
[TODO](https://github.com/) long-term list — when a second driver lands
it gets its own page here.
