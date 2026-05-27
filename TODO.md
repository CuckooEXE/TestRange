# TODO

Convention: items don't get deleted. When something is done or
superseded, it moves to the **Done / Superseded** section at the bottom
with a date stamp.

## Short-term

- `repl`.
- Build + publish the Alpine+dnsmasq sidecar image (volume label
  `TR_SIDECAR_CFG`; init hook copies `/dnsmasq.conf` and `/interfaces`
  into `/etc/`; `qemu-guest-agent` baked in). Publish so users can
  `testrange cache add <url> --name testrange-sidecar` and the smoke
  tests work end-to-end.

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
- Communicators: WinRM, VMware Tools, serial console.
- QGA libvirt-stderr silencer — a process-global `registerErrorHandler`
  is the obvious way to mute "guest agent is not responding" noise
  during the not-yet-up retry, but it's refcounted mutable global
  state. Deferred; revisit only if the noise becomes a real problem.
- IPv6, VLAN tagging, VXLAN, NAT port-forwards.
- `pytest-testrange` plugin.
- Push-only HTTP cache mode for CI.
- Cache eviction (LRU + size cap).
- `Switch(gateway=True)` — implicit router VM for cross-subnet routing
  on the same Switch.
- **`Switch(router=True)`** — make the sidecar act as a router. The
  eventual home for "I want my mgmt switch to route guests to the
  internet without an uplink": sidecar gets `ip_forward=1` + iptables
  MASQUERADE on its uplink, and the dnsmasq config advertises a real
  default gateway via DHCP option 3 (currently always suppressed —
  see `testrange/networks/sidecar.py`). Static netplans get a
  default route too (`NetworkAddressing.default_route`, removed in
  the current rework, would come back). mgmt remains "just a host
  adapter"; router is the active-forwarding capability.
- **Remote-libvirt bridge management.** Today `Switch(internet=True,
  uplink=...)` is local-libvirt only — testrange uses `pyroute2` to
  create the bridge and enslave the NIC, and pyroute2 talks to LOCAL
  netlink. Remote URIs (`qemu+ssh://`, `qemu+tcp://`) + uplink
  switches are caught by preflight (`code="remote_uplink_unsupported"`).
  Options for support: revive `virInterface*` (deprecated netcf),
  open a side-channel SSH for the netlink calls, or ship a small
  agent on the remote host.
- **Multi-subnet mgmt-IPs**. A `Switch(mgmt=True)` with several
  `Network`s currently gets a single `.3/<prefix>` adapter on the
  bridge, derived from the FIRST network on the switch. Guests on
  the other networks see no host adapter on their subnet. Generalize
  to N addresses on the bridge when a plan needs it. See
  `Orchestrator._provision_bridge` for the first-network derivation
  to remove.
- **Host-disconnect preflight warning**. testrange's bridge mgmt
  enslaves a physical NIC chosen by the user — if that NIC is the
  host's only routable interface, the host briefly (or permanently)
  loses network. Not added now (no-new-warnings stance), but worth
  flagging in user docs and possibly an opt-in `--check-uplinks`
  pass.
- Parallel install pass (`ThreadPoolExecutor`); will require per-driver
  `RLock` since `libvirt-python` isn't fully thread-safe.
- Cross-process locking on `state.json` (FileLock) if multiple processes
  ever legitimately need to mutate the same run's state.

## Done / Superseded

- **Switch owns DHCP/DNS/mgmt/internet; sidecar replaces libvirt's
  embedded dnsmasq.** `Switch(dhcp, dns, mgmt, internet)` all default
  False (bare L2). `Network` keeps `name`+`cidr`. One Alpine+dnsmasq
  sidecar per Switch with `needs_sidecar` serves every Network on the
  Switch (one NIC per subnet, one `dhcp-range` per network); the
  rendered `dnsmasq.conf` + Alpine `interfaces` ride in on a tiny config
  ISO (label `TR_SIDECAR_CFG`). Libvirt `<network>` renders without
  `<dhcp>`/`<domain>` — `<ip>` (host at `.1`) emitted iff `internet or
  mgmt`. Install network keeps libvirt-native NAT+DHCP via a separate
  `create_install_network` path. DHCP IP discovery reads the sidecar's
  lease file via QGA (sidecar bakes in `qemu-guest-agent` as a hard
  requirement). `<vm>.<networkname>` DNS resolution lands with it.
  Resolves the long-standing `mgmt` no-op. See PLAN.md §21.
  (2026-05-16)
- **QGACommunicator** — Communicator backed by a hypervisor's native
  guest agent. Driver owns the wire protocol (`_LibvirtGuestAgent` over
  `libvirt_qemu.qemuAgentCommand`), exposed via
  `HypervisorDriver.native_guest_{execute,read_file,write_file}`; the
  communicator is a thin shim over the three loose callables the
  orchestrator brokers in. `GuestAgentError` for agent failures. See
  PLAN.md §20, `examples/qga.py`. (2026-05-14)
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
