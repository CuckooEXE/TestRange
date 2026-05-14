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
- **Proxy abstraction.** Port back the `Proxy` ABC from `.bak/testrange/
  proxy/`: two-shape tunnel into a hypervisor's inner-VM network namespace.
  `connect((host, port)) -> socket.socket` for clients that accept a
  `sock=` (paramiko, requests adapters, asyncio); `forward((host, port),
  bind=...) -> (host, port)` for opaque clients that only know
  `host:port`. Concretes per backend (SSH jumphost for libvirt remote,
  ESXi web console proxy, Proxmox proxy node, ...). Required for any
  Communicator to reach a guest on an inner-only network. Design fresh
  rather than copying `.bak` wholesale.
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

- **Builder-declared readiness hook**, brokered by the orchestrator.
  `Builder.wait_ready(spec, recipe, execute)` on the ABC (non-abstract
  no-op default); the orchestrator hands the builder its VM's `execute`
  callable (`GuestExec`, from `testrange/guest_io.py`) after
  `_bind_communicators`. `CloudInitBuilder` runs `cloud-init status
  --wait` and raises `BuildNotReadyError`. `cloud_init_finished` test
  dropped from `examples/*.py`. See PLAN.md §19.
  (2026-05-13; reshaped argv→callable 2026-05-14)
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
- **Snapshots / per-test revert.** `HypervisorDriver` exposes
  `create_snapshot` / `list_snapshots` / `delete_snapshot` /
  `restore_snapshot`; `LibvirtDriver` implements via
  `snapshotCreateXML` / `revertToSnapshot`; teardown handles
  snapshot-aware cleanup via METADATA_ONLY deletes + pool sweep.
  Per-test revert is the user's call (the snapshot primitive is
  there). (v0.0.1)
