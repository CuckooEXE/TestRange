# ESXi (standalone host)

The ESXi driver runs a portable testrange plan against a **standalone** ESXi
host via pyVmomi (vSphere SOAP). vCenter and distributed switches (DVS) are out
of scope — standard vSwitch + portgroup only
([ADR-0025](../../adr/0025-esxi-standalone-driver.md)).

Install the extra:

```sh
pip install -e '.[esxi]'
```

The control plane is **pyVmomi-only** (vSwitch/portgroup, VM lifecycle,
snapshots, guest-ops, disk realization). Two things ride sanctioned side
channels: volume bytes move over the datastore **`/folder` HTTPS** endpoint
(ESXi has no SOAP byte-egress), and qcow2↔vmdk conversion at the datastore
boundary uses **`qemu-img`** ([ADR-0024](../../adr/0024-qemu-img-disk-conversion.md),
a host binary) — the cache stays qcow2 cache-wide and only the on-datastore
vmdk projection is derived.

## Connecting

A portable plan binds to a host at run time via a connection profile
([ADR-0015](../../adr/0015-backend-binding.md)):

```sh
cp examples/connect.toml.example connect.toml   # gitignored — it holds a password
testrange run --profile esxi tests/plans/generic/lifecycle.py
```

The profile table:

```toml
[esxi]
driver = "esxi"             # required
host = "10.0.0.9"
user = "root"               # optional; defaults to root
password = "Target123!"
port = 443                  # optional
verify_ssl = false          # optional (ESXi ships a self-signed cert)
datastore = "datastore1"    # optional; the VMFS store volumes land on

[esxi.uplinks]
egress = "vmnic1"           # a free physical NIC the NAT sidecar's uplink rides
```

So `host` + `password` is the whole common case. `datastore` defaults to
`datastore1`, `user` to `root`.

## Prerequisites

- **A non-free vSphere license.** The free *vSphere Hypervisor* edition
  restricts the vSphere API to **read-only** — every write
  (`CreateVM_Task`, `AddVirtualSwitch`, snapshots, guest-ops) fails with
  `vim.fault.RestrictedVersion`. A standard vSphere license (or an unexpired
  evaluation) is required. The driver surfaces a failed write as a `DriverError`
  naming the fault.
- **`qemu-img` on the orchestrator host**, for image-origin builds (the qcow2→
  vmdk ingest conversion). Preflight fails loud (`esxi-qemu-img-missing`) if it
  is absent. `apt install qemu-utils` / `dnf install qemu-img` / `brew install
  qemu`. Not needed for installer-origin builds (which land a blank VMFS disk).
- **A free physical NIC for NAT egress.** A `Switch(uplink=…)` with a NAT
  sidecar enslaves the mapped `vmnic` onto a driver-owned uplink vSwitch; that
  NIC must exist and carry upstream connectivity (preflight checks existence as
  `esxi-uplink-pnic-missing`). Isolated switches need no uplink.
- **VMware Tools in the guest, for `NativeCommunicator` VMs.** ESXi's native
  guest agent is VMware Tools, which authenticates against the **guest OS** on
  every call — so a `NativeCommunicator` VM must (a) ship `open-vm-tools` and
  (b) carry a guest credential the orchestrator threads into each call
  (CORE-60). QGA-only images won't work over the ESXi native channel.

## Named uplinks

`Switch(uplink="<name>")` resolves through the profile's `[esxi.uplinks]` map to
a physical NIC ([ADR-0016](../../adr/0016-named-uplinks-out-of-band-egress.md)).
Egress is **out-of-band**: TestRange attaches a NAT sidecar's `eth1` to a
portgroup on the uplink vSwitch but never provisions the NIC's upstream. A pNIC
belongs to one vSwitch, so all NAT switches sharing an uplink share one uplink
vSwitch. An unmapped name fails at preflight.

## `mgmt` semantics (option B)

`Switch(mgmt=True)` gives the **host** an L2 presence at `.2` on the switch's
first network ([ADR-0009](../../adr/0009-mgmt-switch-semantics.md), option B): a
VMkernel NIC on a portgroup of the switch's isolated vSwitch. `.2` is a
**hypervisor-local** reachability guarantee, not promised reachable from a
remote test runner.

## Firmware

`bios` is the certified path; `uefi` is accepted-but-unvalidated. The run-phase
create reproduces the firmware the build used (BUILD-1b).

## Host prerequisites for SSH-communicator VMs

Off-box `SSHCommunicator` VMs are reached by SSH-jumping through the ESXi host
(`guest_gateway`), so the host needs **SSH enabled** with **`AllowTcpForwarding
yes`** in `/etc/ssh/sshd_config`. The host must also carry an L2 presence on the
guest's segment (a `Switch(mgmt=True)` puts a VMkernel NIC at `.2`) for the jump
to reach the guest. `NativeCommunicator` VMs need none of this — VMware Tools
guest-ops ride the SOAP control plane.

VMware Tools VMs (sidecar + `NativeCommunicator` guests) require the guest-ops
plugin: on Alpine that is `open-vm-tools` **plus `open-vm-tools-plugins-all`**
(the base package omits the `vix` plugin and ESXi then rejects guest-ops with
`GuestComponentsOutOfDate`); on Debian the monolithic `open-vm-tools` package
already includes it.

## Certification status

The driver and the **full build→cache→run→test pipeline are proven live** on a
standalone ESXi 8.0.3 host (connect, L2, the qcow2↔vmdk↔VMFS storage round-trip,
VM lifecycle, snapshots, VMware Tools guest-ops, the serial build-result sink,
and end-to-end `hello_world` orchestration through to the build phase).

| Capability | Status |
| --- | --- |
| ESXi driver (ESXI-1…10) | **code-complete**, unit-tested (pyVmomi fakes), gate-green |
| connect + inventory + `/folder` byte I/O | **live-certified** |
| L2 (vSwitch / portgroup / mgmt vmk / uplink) | **live-certified** |
| Datastore storage + qcow2↔vmdk inflate/export round-trip (S2) | **live-certified** (content-verified) |
| VM lifecycle + snapshots | **live-certified** |
| VMware Tools guest-ops (with `open-vm-tools-plugins-all`) | **live-certified** |
| Serial build-result sink + SSH `guest_gateway` | **live-proven** in the end-to-end run |
| Full `tests/plans/` certification sweep on the **physical** host | **blocked: environment egress** — a build VM needs internet for `apt`, but a single-public-IP ESXi host with no internet-connected pNIC and no host-NAT provides no VM-egress path (unlike a Linux/PVE host's NAT bridge). Not a driver defect; needs an egress path provisioned. |
| Full `tests/plans/` sweep on a **nested ESXi** | **lab path, shelved post-1.0.0** — a `GuestHypervisor.esxi` plan stands up an ESXi node on the libvirt L0 (ADR-0021 amendment) and certifies against it, sidestepping the egress block: inner VM disks build on the L0 (NAT egress) and the nested ESXi only boots pre-built disks. **Live status (2026-06-02):** the ESXi 8.0U3b install runs unattended to a bootable DCUI on the L0 once the installer CD is on IDE (BACKEND-13) and the kickstart heredoc is flat (BUILD-22), but `%firstboot` does not take effect on the installed guest (ESXI-17). Per the REL epic the standing ESXi cert instead runs on a raw-kickstart host (REL-11); this nested path is shelved. |
| vCenter / DVS / dvportgroup | out of scope ([ADR-0025](../../adr/0025-esxi-standalone-driver.md)) |

### Nested ESXi (lab certification)

Because the physical host can't give build VMs internet egress, the portable
`tests/plans/` corpus can be certified against a **nested ESXi** instead — an
ESXi node installed unattended (kickstart, with `license=` applied via
`serialnum`) as a guest on the libvirt reference backend, which *does* have NAT
egress. This is a lab path: per the REL epic the standing ESXi cert runs on a
raw-kickstart host (REL-11), and this nested approach is shelved post-1.0.0. The
[nested-virtualization model](../../adr/0021-nested-virtualization.md) builds the
inner (L2) VM disks on the L0 with real egress and then only *boots* them on the
nested ESXi, so no VM-egress path is needed on the ESXi node itself. The guest
declares ESXi-compatible hardware via the libvirt-concrete device types (a
`LibvirtOSDrive(bus="sata")` + `LibvirtNetworkIface(model="e1000e")`,
[ADR-0026](../../adr/0026-libvirt-concrete-device-types.md)) since ESXi has no
virtio drivers, and `CPU(nested=True)` so the L0 exposes VMX for the guest's own
VMs. The installer CD-ROM is attached on **IDE** (not sata): on BIOS/i440fx,
weasel's `ks=cdrom:` scan only enumerates an IDE optical unit. The cert plan is
a bespoke `GuestHypervisor.esxi` topology (not a shipped example), run against
the libvirt profile with a raised build timeout:

```sh
export TESTRANGE_ESXI_LICENSE=XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
testrange run --build-timeout 1800 --profile libvirt-local <nested-esxi-plan>.py
```

`--build-timeout` is raised from the 600s default because an ESXi install +
reboot + `%firstboot` under nested KVM takes well over ten minutes.
