# TODO — Kanban Board

A lightweight kanban board. **Columns** are status (`## Backlog → ## To Do →
## In Progress → ## Done`); **swimlanes** (`###`) group tickets by workstream.
Tickets are discrete and never deleted — they flow rightward to **Done** with a
date stamp.

**Ticket format:** `<type> | <ID>: Brief description` + an indented detail line.

- **Types:** `feat` (feature) · `bugfix` · `chore` · `ci` · `test` · `docs`.
- **Swimlane ID prefixes:** `PVE` Proxmox driver · `BACKEND` other backends ·
  `NET` networking · `CACHE` cache · `ORCH` orchestrator · `CORE` plan/data
  types · `COMM` communicators · `BUILD` builders · `PROXY` reachability ·
  `DOCS` documentation · `CI` tooling/chore.

---

## In Progress

### PVE — Proxmox driver
- **feat | PVE-1: Proxmox `HypervisorDriver` concrete + `ProxmoxHypervisor` Plan entry**
	Wire `drivers/proxmox/{_client,_naming,_sdn}.py` into a `ProxmoxDriver(HypervisorDriver)`,
	register it with `drivers/_registry.py`, and add the `ProxmoxHypervisor`
	dataclass as a top-level Plan entry. Branch: `feature/proxmox`.

---

## To Do

### NET — Networking
- **bugfix | NET-1: `validate.py` hardcodes the user-static pool bounds**
	`testrange/networks/validate.py:188-189` interpolates `network_address + 100`
	/ `+ 254` into the DHCP-pool overlap error instead of importing
	`USER_STATIC_LO` / `USER_STATIC_HI` from `_addressing_consts.py` — the exact
	drift those constants exist to prevent.

### CORE — Plan / data types
- **chore | CORE-1: drop dead `Plan` dataclass field defaults**
	`testrange/plan.py:18-19` (`hypervisors = field(default_factory=tuple)`,
	`name: str = ""`) are inert — the hand-written `__init__` overrides
	construction and raises on an empty name. Misleading (implies `Plan()` is
	constructible). Remove them or comment why they're kept.

### CI — Tooling / chore
- **chore | CI-1: SHA-stamp + version-track the sidecar image**
	`tools/build-sidecar-image/build.sh` produces an unstamped
	`testrange-sidecar.qcow2` with unpinned Alpine packages. The build cache key
	folds the rendered seed text, not the sidecar image's hash, so a drifted
	sidecar silently invalidates nothing. Stamp a content SHA + version into the
	artifact and fold it into the key.

---

## Backlog

### PVE — Proxmox driver
- **feat | PVE-2: Proxmox L2 via SDN (`create_switch` / `create_network`)**
	Realize a Switch as an SDN zone + vnet (stage → **apply**); attach Networks
	as vnets. For `nat + uplink`, the uplink-facing segment for the sidecar's
	`eth1`. The orchestrator never names a bridge (ADR-0008 §1).
- **feat | PVE-3: Proxmox pool I/O (`upload_to_pool` / `download_from_pool`)**
	Upload via the proxmoxer file API (iso/import path); download has no REST
	endpoint → open a paramiko SFTP channel to the node. Constrained to
	`dir`/`nfs` pools so `compose_volume_ref` stays filename-deterministic
	(ADR-0008 §6).
- **feat | PVE-4: Proxmox native guest agent transport**
	QGA over the PVE API (async pid + poll, no stdin, size limits → chunk
	writes). Back `NativeCommunicator`; declare `native_guest_capabilities()`.
- **feat | PVE-5: Proxmox snapshots incl. memory**
	`create_snapshot` with `vmstate=1`; map to the snapshot ABC + LIFO teardown.
- **feat | PVE-6: Proxmox name → (node, vmid) resolution**
	vmid is allocated at create time; stamp the composed name into the VM
	`name`/notes so teardown recovers the handle with no external map (ADR-0008 §6).
- **test | PVE-7: Proxmox integration suite**
	Tests behind the `proxmox` pytest mark, gated on `TESTRANGE_PVE_HOST`.

### BACKEND — Other backends
- **feat | BACKEND-1: libvirt driver rebuild against the multi-backend ABC**
	Re-implement the deleted libvirt driver (`libvirt-python`) to the current ABC:
	`create_switch` via host bridges, QGA native transport, stream-based pool I/O.
- **feat | BACKEND-2: ESXi driver**
	`pyVmomi`; vSwitch + portgroup (DVS + dvportgroup for vCenter); VMware Tools
	guest-ops (needs guest creds); `/folder` HTTPS pool I/O.
- **feat | BACKEND-3: Hyper-V driver**
	WMI (`Msvm_*`) + PowerShell Direct for in-guest ops; per-vNIC VLAN; SMB/WinRM
	pool transfer.
- **chore | BACKEND-4: QGA libvirt-stderr silencer**
	Mute "guest agent is not responding" retry noise via a process-global
	`registerErrorHandler`. Refcounted mutable global state — rides BACKEND-1.
- **feat | BACKEND-5: remote-libvirt L2 over `qemu+ssh://`**
	Host-local netlink can't reach a remote URI; needs `virInterface*`, an SSH
	side-channel, or a remote agent. Rides BACKEND-1.

### NET — Networking
- **feat | NET-2: `Switch(router=True)` — sidecar as router**
	Sidecar gets `ip_forward=1` + nftables MASQUERADE on its uplink, and dnsmasq
	advertises a real default gateway via DHCP option 3 (currently suppressed —
	`testrange/networks/sidecar.py`). mgmt stays a host adapter; router is the
	active-forwarding capability.
- **feat | NET-3: `Switch(gateway=True)` — implicit router VM**
	Cross-subnet routing between Networks on the same Switch via an implicit
	router VM.
- **feat | NET-4: multi-subnet mgmt IPs**
	A `Switch(mgmt=True)` derives its single `.2` adapter from the first network;
	generalize to N host adapters when a plan needs it.
- **feat | NET-5: IPv6 / VLAN tagging / VXLAN / NAT port-forwards**
	L2/L3 features beyond the current IPv4 + flat-subnet model.
- **chore | NET-6: host-disconnect preflight warning (`--check-uplinks`)**
	Enslaving the host's only routable NIC drops the host off the network; warn
	at preflight in an opt-in pass.

### CACHE — Cache
- **feat | CACHE-1: push-only HTTP cache mode for CI**
	ADR-0010 §5 added best-effort upstream push of built disks; add a dedicated
	push-only mode for build farms.
- **feat | CACHE-2: cache eviction (LRU + size cap)**
	Bound the local cache; evict least-recently-used entries past a size cap.

### ORCH — Orchestrator
- **feat | ORCH-1: multiple top-level Hypervisors in a Plan**
	`Plan(*hypervisors)` is already variadic; lift the v0 "exactly one" runtime
	check and broker across backends.
- **feat | ORCH-2: nested orchestration**
	`AbstractHypervisor` shape designed fresh (not copied from `.bak`).
- **feat | ORCH-3: `--resume <run_id>`**
	State schema is already future-proofed (intent_at/outcome_at + metadata);
	wire the runtime to resume a partially-built run.
- **feat | ORCH-4: parallel build pass**
	`ThreadPoolExecutor` over independent VMs; needs per-driver locking since some
	backend SDKs aren't thread-safe. Deferred in ADR-0010.
- **feat | ORCH-5: cross-process locking on `state.json`**
	`FileLock` if multiple processes ever legitimately mutate the same run.

### BUILD — Builders
- **feat | BUILD-1: installer-based OS-disk origin**
	`Builder.materialize_os_disk()` seam (named in ADR-0010 §6): blank OS disk +
	boot media for ESXi Kickstart / Windows autounattend.
- **feat | BUILD-2: Proxmox answer-file builder**
	A builder that renders a Proxmox/Debian preseed-style answer file.

### COMM — Communicators
- **feat | COMM-1: WinRM communicator**
	For Windows guests reachable over the network.
- **feat | COMM-2: VMware Tools communicator**
	Guest-ops over VMware Tools (pairs with BACKEND-2).
- **feat | COMM-3: serial console communicator**
	For guests with no network and no native agent.

### PROXY — Reachability
- **feat | PROXY-1: `Proxy` ABC ported fresh from `.bak`**
	Two-shape tunnel into a hypervisor's inner-VM network namespace:
	`connect((host,port)) -> socket` for `sock=`-accepting clients (paramiko,
	requests, asyncio) and `forward((host,port), bind=...) -> (host,port)` for
	opaque clients. Concretes per backend (SSH jumphost, ESXi web console proxy,
	Proxmox proxy node). Required for any Communicator to reach an inner-only VM.

### CORE — Plan / data types
- **feat | CORE-2: cross-format disk conversion (qcow2 ↔ vmdk ↔ raw)**
	Re-introduces a sanctioned `qemu-img` subprocess module behind its own ADR
	(subprocess is otherwise banned — ADR-0001).
- **feat | CORE-3: `pytest-testrange` plugin**
	Expose ranges + tests as pytest fixtures/items.

---

## Done

### 2026-05-22
- **ci | CI-2: pre-commit pytest hook filtered on a stale `libvirt` marker**
	`.pre-commit-config.yaml`'s pytest hook ran `pytest -m "not libvirt"`; the
	only registered marker is now `proxmox`. Changed to `-m "not proxmox"` (+ hook
	name). Verified: hook runs green, 404 tests pass.
- **chore | CHORE-CLEANUP: repo-wide TODO / PLAN / docs cleanup**
	Retired the libvirt-era audit (OBE under ADR-0008/0010); rewrote PLAN.md to
	current truth (MockHypervisor, build/run split, regenerated file tree);
	swept docs/README/docstrings (deleted `docs/user/drivers/libvirt.md`,
	`QGACommunicator`→`NativeCommunicator`, `install`→`build`, fixed the broken
	`libvirt` extra → `proxmox`). Suite green (404 tests), ruff + mypy clean.

### 2026-05-22 (ADR-0010)
- **feat | ORCH-DONE: build/run split (Phases B0–B6)**
	`build_phase` warms the cache and nothing else; `run_phase` creates pools,
	gates sidecar readiness, pushes every built disk (OS + data) per VM, runs
	tests. `testrange build` / `testrange run` (auto-build on miss;
	`--require-cache`) are distinct CLI verbs. `config_hash` keys the disk set;
	`create_blank_volume` + `resize_volume` replaced `create_disk_from_base`.

### 2026-05-21 (ADR-0008)
- **feat | BACKEND-DONE: multi-backend driver ABC**
	Driver owns the Switch (`create_switch`); `MockDriver` is the reference
	backend; `QGACommunicator` → `NativeCommunicator`; native-capability +
	pool-capacity preflight. The original libvirt driver was deleted (rebuild =
	BACKEND-1).
- **feat | NET-DONE: `NetworkIface.addr` sum type + `nic_idx`**
	`addr: DHCPAddr | StaticAddr | None`; `None` → unconfigured (`dhcp4: false`),
	`DHCPAddr()` → lease, `StaticAddr(...)` → static (explicit-wins resolution).
	`SSHCommunicator(nic_idx=)` selects the NIC by position. Fixed the
	`dhcp4:true`-for-no-DHCP-NIC bug.

### Earlier (v0.0.1 – 2026-05-16)
- **feat | ORCH-DONE: Switch owns DHCP/DNS/mgmt; per-Switch dnsmasq sidecar**
	(ADR-0009) Sidecar replaces backend-native dnsmasq; lease discovery over the
	native guest agent.
- **feat | ORCH-DONE: builder readiness hook, stable MACs, snapshots,
  deterministic `config_hash`, cleanup on all exceptions**
	(ADR-0006, ADR-0007) See PLAN §16/§19 and the ADRs.
