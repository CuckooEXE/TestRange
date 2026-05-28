# ADR-0008: Driver ABC shape for Proxmox / libvirt / VMware / Hyper-V

Status: Accepted
Date: 2026-05-21

## Context

`HypervisorDriver` (the ABC every backend implements) was grown against a
single backend (libvirt). Before the libvirt driver is rewritten we validated
the ABC against the four backends we actually intend to ship, by surveying each
SDK/API:

- **Proxmox** — `proxmoxer` over the PVE REST API.
- **libvirt** — `libvirt-python`.
- **VMware** — `pyVmomi`, in two scenarios: a standalone ESXi host, and a
  vCenter orchestrating one or more ESXi hosts.
- **Hyper-V** — WMI (`Msvm_*` in `root\virtualization\v2`), plus PowerShell
  Direct for in-guest operations.

The invariants we want to hold across all four:

1. Each driver exposes its own **native communicator** (Proxmox QGA, libvirt
   QGA, VMware Tools guest-ops, Hyper-V integration / PowerShell Direct).
2. **Minimal per-driver setup** — install the OS, nothing else; the driver
   builds its own L2. No "build the bridge first."
3. **Full cleanup** — VMs, networks/switches, pools, and disks all deleted per
   run.
4. **Caching lives on the runner side, never on the driver backend.**

Verdict: the method *set* is largely correct and the optional-capability escape
hatch (`native_guest_*` raising `DriverError`) is the right instinct. But
several contracts encoded libvirt-isms that break the other three backends. The
sharpest problem was not in `base.py` — it was in `orchestrator/provision.py`,
which computed Linux-bridge topology and threaded a `bridge_name` into
`create_network`, making the *orchestrator* libvirt-shaped (violating invariant
#2 for ESXi/Proxmox/Hyper-V).

### Capability matrix (survey result)

Legend: ✅ clean · ⚠️ works with friction the driver absorbs · ❌ contract as
written does not hold; needs a change or out-of-band channel.

| Contract | libvirt | Proxmox | ESXi-only | vCenter | Hyper-V (WMI) |
|---|---|---|---|---|---|
| `backend_name` = opaque string handle | ✅ | ⚠️ needs name→(node,**vmid**) map; stamp name into VM `name`/notes | ✅ | ✅ | ⚠️ store **GUID**; resolve name→GUID |
| Native exec | ✅ QGA | ✅ QGA (async pid+poll, no stdin, size limits) | ⚠️ Tools **+ guest creds**; stdout via redirect+download | same | ⚠️ **PS-Direct + creds** (Win/Linux via integration svc); not raw WMI |
| Native read file | ✅ | ✅ (16 MiB cap) | ⚠️ Tools+creds (URL GET) | same | ⚠️ PS-Direct + creds; not raw WMI |
| Native write file | ✅ | ✅ (~tens-of-KB write cap → chunk) | ⚠️ Tools+creds (URL PUT) | same | ⚠️ PS-Direct or `CopyFilesToGuest` (host→guest) |
| Driver builds L2 (Switch + Networks) | bridge+net | vmbr **or** SDN zone/vnet (stage→**apply**) | vSwitch+portgroup | **DVS**+dvportgroup (multi-host) | VMSwitch + **per-vNIC VLAN** (no portgroup) |
| `create_pool`/`destroy_pool` is a real object | ✅ | ⚠️ storage def (data residue caveats) | ❌ pool = **dir in pre-existing datastore** | same | ❌ pool = **dir/share on host** |
| `upload_to_pool` / `download_from_pool` via SDK | ✅ stream | ⚠️ **upload** via proxmoxer API (iso/tmpl/import); **download** scp/SSH | ✅ HTTPS `/folder` PUT/GET | ✅ (via vCenter) | ❌ no WMI transfer; SMB/WinRM |
| `compose_volume_ref` pure/deterministic | ✅ | ⚠️ only for **file/dir** storage (block volids are derived) | ✅ | ✅ | ✅ (.vhdx paths) |
| Snapshot incl. memory (`mem=True`) | ✅ | ✅ `vmstate=1` | ✅ | ✅ | ✅ Full(2) |
| Seed-ISO first boot (attach CDROM/DVD) | ✅ | ✅ | ✅ | ✅ | ✅ DVD |
| Async op model | sync | UPID poll | Task | Task | Job 4096 |

### What holds unchanged

- VM lifecycle, snapshots (incl. memory), `compose_mac` (see ADR-0006),
  `seed_iso_ref` first-boot, and the LIFO state-driven teardown
  (ADR-0003) all map cleanly across all four backends.
- vCenter **placement** (datacenter/cluster/pool/datastore/folder) is
  resolvable as *static driver config* on the per-driver hypervisor dataclass,
  not per-`create_vm`. ESXi-only is the trivial subset. `create_vm` stays
  placement-free.
- Async backends block to completion *inside* driver methods; the ABC stays
  synchronous.

## Decision

### 1. The driver owns the Switch; the orchestrator never names a bridge

Add `create_switch(switch, backend_name)` / `destroy_switch(backend_name)` to
the ABC. The driver realizes the full L2 topology for a Switch — the isolated
guest segment and, when `switch.uplink and switch.nat`, the uplink-facing
segment the sidecar's second NIC rides. `create_network(network, switch,
backend_name, *, switch_backend_name)` attaches a network to an
already-created switch; the `bridge_name` parameter and the
`compose_bridge_name`/`create_bridge`/`create_isolated_bridge`/`destroy_bridge`
methods are **removed** from the ABC (they were libvirt-internal). The
orchestrator's `provision_switch` loses all bridge-topology branching and the
`__uplink__<switch>` synthetic-Switch hack; `destroy()` dispatches
`switch`/`install_switch` → `destroy_switch`.

### 2. Native communicators take optional guest credentials

QGA is unauthenticated; VMware Tools and Hyper-V PowerShell Direct require
per-call guest OS credentials. The three accessors gain an optional
`credential` keyword:

```
native_guest_execute(backend_name, *, credential=None) -> GuestExec
native_guest_read_file(backend_name, *, credential=None) -> GuestReadFile
native_guest_write_file(backend_name, *, credential=None) -> GuestWriteFile
```

QGA drivers ignore it; VMware/Hyper-V require it. The orchestrator brokers the
credential (already a Plan concept) at run-phase bind, preserving the stovepipe
rule.

### 3. Native capability is declared and validated in preflight

> **Amended 2026-05-27 (CORE-16) — see Addendum:** this section is rescinded for
> now. `native_guest_capabilities()` and the preflight check that consumed it
> were removed; the native-agent *transport* is unchanged.

Native ops are not uniformly available. The driver declares
`native_guest_capabilities() -> frozenset[str]` (subset of
`{"execute","read_file","write_file"}`, default empty). Preflight fails loud
(`PreflightFinding`) when a VM's communicator needs an op the driver does not
declare — never a mid-run surprise.

### 4. DHCP-lease discovery does not assume native read

`discover_ip` no longer hard-requires `native_guest_read_file` on the sidecar.
Lease discovery is an explicit channel that defaults to native read where the
driver declares it and otherwise falls back (SSH to the sidecar over its mgmt
IP, or static-only addressing). DHCP lease lookup remains *not* a driver method
— the sidecar owns DHCP.

### 5. A "pool" is a namespace in pre-existing storage; `size_gb` is a floor check

`create_pool`/`destroy_pool` create/remove a named namespace inside
pre-existing backing storage (libvirt storage pool, Proxmox storage def, ESXi
datastore subdirectory, Hyper-V host dir/share) — they do not provision
storage. The backing store is static driver config. `StoragePool.size_gb`
stays required and becomes a **preflight minimum-capacity check**: the driver
confirms the backing store has at least `size_gb` free in a single store (ESXi
→ one VMFS datastore; libvirt → the pool filesystem; Hyper-V → the volume
behind the dir/share). It is a precondition we verify, not a quota we impose.

### 6. Documented driver responsibilities (no signature change)

- **`backend_name` discoverability:** the orchestrator records its
  deterministic composed name *before* create (crash-safe teardown, ADR-0003).
  Backends whose real handle is allocated at create time (Proxmox vmid, Hyper-V
  GUID) must make the composed name recoverable from the backend (stamp into VM
  name/notes/tags; resolve on `destroy`) so teardown needs no external map.
- **`compose_volume_ref` purity** holds only for file/dir storage → the Proxmox
  driver is constrained to `dir`/`nfs` pools (controllable filenames), not
  block.
- **Out-of-band transport reality:** invariant #4 requires every driver to have
  *some* host file-transfer channel for `upload_to_pool` /
  `download_from_pool`. SDK-native for libvirt (stream) and ESXi (`/folder`
  HTTPS); Proxmox uploads via the proxmoxer file API (import path) but has no
  download API (scp/SSH); Hyper-V uses SMB/WinRM. The endpoints/creds live in
  those drivers' config. "API only" is not universally achievable, and that is
  expected.

  **Amendment (2026-05-24, PVE-17 / ADR-0012).** The Proxmox driver gains a
  sanctioned non-proxmoxer transport beyond SFTP: a `vncwebsocket` connection
  (`websocket-client`) for reading the build-result serial console. PVE serves
  `serial0` only over `termproxy`→`vncwebsocket` (no REST GET), so the
  build-result sink (ADR-0012) needs it. Accepted for the live fast-fail + live
  build output it buys. Constraint: it requires password-ticket auth (termproxy
  rejects API tokens), so it forecloses moving the driver to API-token auth
  unless serial reverts to the documented disk-over-SFTP fallback (RESEARCH.md
  "PVE-16 spike").

  **Amendment (2026-05-24, PVE-23).** Volume **uploads** moved to SFTP too, so
  *all* volume bytes now ride SFTP (both directions); proxmoxer is the control
  plane only. PVE's REST `upload` endpoint returns `501 "for data too large"` on
  large `import` disk images (hit on the first live run — proxmoxer already
  streams >10 MiB, so it's a server-side endpoint limit, not client buffering).
  `upload_to_pool`/`write_to_pool` `sftp_put` the file into the storage content
  dir, where a `dir`/`nfs` store discovers it by scan — the same volid the REST
  upload would have produced. The table row above ("upload via proxmoxer API")
  is superseded for the disk path.

### 7. The native communicator is named for the concept, not QEMU

`QGACommunicator` is renamed `NativeCommunicator` — a backend-agnostic shim
that binds three driver-supplied callables. "QGA" is QEMU-specific; the same
shim fronts VMware Tools and Hyper-V integration. The per-backend wire protocol
lives in the driver, never in the communicator.

## Consequences

- The libvirt driver (and the libvirt-bound QGA wire) is deleted and rebuilt
  later against this ABC; in the interim a `MockDriver` + mock native transport
  is the test substrate.
- The orchestrator stops knowing bridges exist — L2 logic that was in
  `provision.py` moves *into* each driver.
- Preflight gains two new failure classes (native-capability gap, pool-capacity
  floor), turning two former mid-run/late failures into early, actionable ones.
- A backend lacking unauthenticated native read (Hyper-V Linux sidecar) is, for
  v1, constrained to static addressing unless its lease channel is wired.

## Addendum — 2026-05-27 (CORE-16)

§3 ("Native capability is declared and validated in preflight") is **rescinded
for now**. `native_guest_capabilities()` and the preflight check that consumed
it (`preflight.native_capability_findings`) are removed. In practice the gate
fired in only two cases: a `NativeCommunicator` (or DHCP-lease readback) on
libvirt before its QGA transport lands (BACKEND-1.5), since libvirt inherited
the empty base default; and `MockDriver`'s test-only capability knob — Proxmox
already declares the full op set, so it never tripped. Rather than carry the
machinery for those, it is removed.

What stays: the native-agent *transport* of §1/§2 — `native_guest_execute` /
`native_guest_read_file` / `native_guest_write_file` and the optional
`credential` keyword — is unchanged. A backend that cannot perform an op simply
leaves that accessor at its default, which raises `DriverError`. The trade is
that this libvirt-before-1.5 case now fails at first use rather than at
preflight — accepted, since it's transient and loud either way.

Consequently the "native-capability gap" preflight failure class listed under
Consequences no longer exists (the pool-capacity floor remains). The per-op
preflight gate will be reinstated when a backend that genuinely lacks an op
lands (Hyper-V / WinRM) — at which point this section is un-rescinded or a new
ADR supersedes it.
