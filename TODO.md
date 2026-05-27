# TODO

Convention: items don't get deleted. When something is done or
superseded, it moves to the **Done / Superseded** section at the bottom
with a date stamp.

## Short-term (in scope for v0)

- DHCP-on-by-default per Network.
- DNS via per-network dnsmasq; `<vm>.<network>` resolution.
- `mgmt=True` on Switch/Network: places a management interface so guests
  can reach the host's libvirt API. Document the security implication
  (guests can reach the host).
- `internet=True` (default) / `air_gapped=True` on Switch/Network:
  NAT-to-host vs internal-only.
- Intelligent cleanup on ALL exceptions, including CTRL-C (via signal
  handler that transitions to cleanup, not via `atexit`). `kill -9` is
  recoverable only via state-file-driven `testrange cleanup`.
- `Builder.config_hash`: deterministic password-salt seed to keep cache
  hits across runs.
- **Driver-level stable MAC assignment.** Each driver derives a NIC's
  MAC deterministically from `(plan_name, vm_name, nic_index)` so the
  install VM and run VM (and any re-run of the same plan) get the same
  MAC. Required because cloud-init's rendered network-config on the
  cached disk can match interfaces by MAC — letting the backend
  auto-generate would silently break networking on every cache-hit run.
  This belongs in the driver, not in shared utility code: each backend
  has its own OUI and MAC-format conventions (libvirt/KVM `52:54:00:…`,
  VMware `00:50:56:…`, Hyper-V `00:15:5D:…`). The driver ABC exposes
  the contract; concretes own the hash + prefix.

## Long-term

- Multiple top-level Hypervisors in a Plan.
- Nested orchestration (`AbstractHypervisor` shape designed fresh, not
  copied from `.bak`).
- `--resume <run_id>` (state schema already future-proofed).
- Snapshots / per-test revert.
- Drivers: Proxmox, ESXi, Hyper-V.
- Remote hypervisor support (`qemu+ssh://` etc.) — re-introduces a
  storage-transport abstraction.
- Cross-format disk conversion (qcow2 ↔ vmdk ↔ raw) — re-introduces a
  sanctioned `qemu-img` subprocess module with its own ADR.
- Builders: Proxmox answer-file, ESXi kickstart, Windows unattended.
- Communicators: QGA, WinRM, VMware Tools, serial console.
- IPv6, VLAN tagging, VXLAN, NAT port-forwards.
- `pytest-testrange` plugin.
- Push-only HTTP cache mode for CI.
- Cache eviction (LRU + size cap).
- `Switch(gateway=True)` — implicit router VM for cross-subnet routing
  on the same Switch.
- Parallel install pass (`ThreadPoolExecutor`); will require per-driver
  `RLock` since `libvirt-python` isn't fully thread-safe.
- Cross-process locking on `state.json` (FileLock) if multiple processes
  ever legitimately need to mutate the same run's state.

## Done / Superseded
