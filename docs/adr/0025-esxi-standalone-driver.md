# ADR-0025: ESXi standalone-host driver — scope & transports

Status: Accepted
Date: 2026-06-01

Records the load-bearing decisions for the ESXi backend (`drivers/esxi/`, EPIC
ESXI). Recon that informed it: `notes/esxi/S1-recon.md` (live pyVmomi survey of
ESXi 8.0.3).

## Context

ESXi joins libvirt (reference, ADR-0019) and Proxmox as a `HypervisorDriver`
backend. The target is a **standalone** ESXi host (`apiType == "HostAgent"`), not
vCenter. The SDK is pyVmomi (vSphere SOAP). Several ESXi realities differ sharply
from the qcow2/QGA backends and force decisions.

## Decision

### Scope — standalone host only

- **Standard vSwitch + portgroup**; DVS/dvportgroup are vCenter-only and **out of
  scope** (a future vCenter-aware driver). `connect` asserts `apiType ==
  "HostAgent"` and fails loud on a vCenter endpoint.
- The host's singletons are resolved at connect: one HostSystem, one
  ComputeResource, one root ResourcePool, one datacenter (`ha-datacenter`).

### Transports

- **pyVmomi SOAP** is the control plane: vSwitch/portgroup reconfigure, VM
  lifecycle (`CreateVM_Task`), snapshots, guest-ops, and disk realization
  (`VirtualDiskManager`).
- **Datastore `/folder` HTTPS** is the volume byte channel (PUT/GET) — ESXi has
  no SOAP byte-egress for datastore files. This is the ESXi analog of the
  Proxmox SFTP exception; it carries seed ISOs (write_to_pool), the ingest
  staging vmdk, and the export read-back.
- **`qemu-img`** (host binary, ADR-0024/CORE-2) converts qcow2↔vmdk at the
  driver boundary. Not a SOAP transport; a host-side conversion step.

### Disk format (decision A, 2026-06-01)

Canonical cache format is **qcow2** cache-wide. ESXi (VMFS = vmdk) converts only
at its boundary:

- **ingest** (`upload_to_pool`): qcow2 → `monolithicSparse` vmdk (single
  self-contained transport file) → `/folder` PUT staging →
  `CopyVirtualDisk_Task` inflate to a managed VMFS **thin** disk that is
  **bootable and growable** (`ExtendVirtualDisk_Task` for `resize_volume`). The
  `streamOptimized` subformat is transport-only — not runnable until inflated;
  `CopyVirtualDisk` from a hosted-sparse source is the inflate (S2).
- **egress** (`download_from_pool`): the disk is attached in place, so its ref
  denotes the exact file the VM wrote; GET its descriptor + `-flat` extent →
  `qemu-img` vmdk → qcow2 (self-contained, ABC no-backing-chain invariant).
- The on-datastore vmdk is **derived, ephemeral, never content-addressed**
  (qemu-img vmdk output is not byte-deterministic); only the qcow2 is keyed by
  content.

Disks live at their **pool-folder ref path** and are attached in place, so a
stable `VolumeRef` denotes the same file across `upload → create_vm → download →
delete` — no Proxmox-style Option-2 vm-scoped re-resolution.

### Guest-ops — VMware Tools + the per-call credential seam

The native agent is `GuestOperationsManager` over SOAP. Unlike QGA, VMware Tools
authenticates against the **guest OS** on every call, which forces the optional
`credential` kwarg the ABC deferred (CORE-60, ADR-0008). Two VMware-Tools facts
shape `_guest.py`:

- `StartProgramInGuest` captures no stdout/stderr — `execute` runs under
  `/bin/sh -c` with output redirected to guest temp files, polls
  `ListProcessesInGuest` for the exit code, then reads the files back over the
  guest file-transfer HTTPS side-channel. A guest shell + `open-vm-tools` is
  required (the portable cloud image must install it — ESXI-12).
- file I/O is a one-time-URL HTTPS transfer (`Initiate*FileTransfer*`).

### Serial build-result sink

A datastore-file-backed `VirtualSerialPort` (`[ds] <vm>/serial0.log`) is tailed
over `/folder` Range GETs with `b""` heartbeats and EOF on power-off
(ADR-0012 serial-everywhere; S3).

### MACs

A `manual` NIC MAC must fall in VMware's range `00:50:56:00:00:00`–
`00:50:56:3f:ff:ff` (the host rejects anything else), so ESXi's `compose_mac`
uses that OUI with the 4th octet masked to `0x3f` — distinct from the
locally-administered `0x02` OUI the other backends use.

### Firmware

**bios certified; uefi accepted-but-unvalidated.** Both are in
`SUPPORTED_FIRMWARES` (preflight has no warning tier, so uefi is not blocked);
the unvalidated caveat is documentation. `ConfigSpec.firmware` maps
`bios`/`efi`. The run-phase create reproduces the build firmware (BUILD-1b).

## Consequences

- A new host-binary runtime dep (`qemu-img`, ADR-0024) for image-origin ESXi
  builds; preflight gates its absence (ESXI-9).
- The portable `examples/capabilities.py` needs ESXi-aware adjustments (ESXI-12):
  `open-vm-tools` in the native image, guest credentials for VMware Tools, and
  disk device naming that isn't virtio-specific (`/dev/sd*`, not `/dev/vd*`).
- Licensing: the vSphere **API write** path requires a non-free license (the free
  "vSphere Hypervisor" edition is read-only). Documented in the setup page
  (ESXI-14).

## Alternatives considered

- **vim-cmd/esxcli/vmkfstools over SSH.** The free license does not gate the
  host CLI, so an SSH-driven control plane would dodge the licensing requirement
  — but it contradicts the pyVmomi-only design and the subprocess ban
  (ADR-0001), and would be a second, ESXi-only subprocess surface. Rejected;
  reconsider only if a no-license posture becomes a hard requirement.
- **HttpNfcLease ImportVApp for ingest.** Posts a `streamOptimized` stream to a
  lease URL and creates the VM+disk in one shot — but it couples disk ingest to
  VM creation, which breaks the orchestrator's disk-first/`create_vm`-later
  model. `CopyVirtualDisk` from a `/folder`-staged hosted-sparse keeps the two
  separable.
