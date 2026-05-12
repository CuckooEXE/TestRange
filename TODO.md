# TODO

Convention: items don't get deleted. When something is done or
superseded, it moves to the **Done / Superseded** section at the bottom
with a date stamp.

## Short-term

- `repl`.
- DNS via per-network dnsmasq with `<vm>.<network>` resolution. The
  `Network.dns` flag is honored at the libvirt XML level (forward DNS),
  but the `<vm>.<network>` short-name resolution piece isn't wired.
- `mgmt=True` on Switch is accepted at Plan time but is **not currently
  honored by `LibvirtDriver`** — fix is to render a management
  interface so guests can reach the host's libvirt API. Document the
  security implication (guests can reach the host) when wiring.

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

- **DHCP-on-by-default per Network.** `Network.dhcp` defaults to `True`;
  `LibvirtDriver` renders DHCP in the network XML. (2026-05-11)
- **`internet=True` (default) / `internet=False` on Switch.** Switch's
  `internet` flag is honored at the libvirt XML level: `True` renders
  `<forward mode='nat'/>`; `False` renders no forward (air-gapped).
  (2026-05-11)
- **Intelligent cleanup on ALL exceptions, including CTRL-C.**
  `Orchestrator._install_signal_handlers` raises `KeyboardInterrupt` on
  SIGTERM/SIGHUP, routing through `__exit__`'s cleanup path. `kill -9`
  is recoverable via state-file-driven `testrange cleanup`. (2026-05-11)
- **`Builder.config_hash` deterministic across runs.** Superseded by
  deterministic Ed25519 keypair derivation: `gen_ssh_key(comment=...)`
  seeds from `sha256(comment)`, so the rendered seed (and thus
  `config_hash`) is byte-stable. (v0.0.1)
- **Driver-level stable MAC assignment.** `LibvirtDriver.compose_mac`
  derives a stable MAC from `(plan_name, vm_name, nic_index)` under
  the KVM `52:54:00:` OUI. See ADR-0006. (2026-05-11)
