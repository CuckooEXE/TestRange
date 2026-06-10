# Changelog

All notable changes to this project are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project follows [Semantic Versioning](https://semver.org/) from 1.0.0.

## [Unreleased]

## [1.1.1] — 2026-06-09

### Fixed

- ``examples/nested_lab.py`` — the nested host's inner plan now declares its own
  NAT ``build_switch`` so the inner VMs reach the internet for ``apt`` during their
  L0 build. Without it the inner build booted on the default isolated, no-egress
  build network and ``apt`` could not resolve its mirror.

## [1.1.0] — 2026-06-09

### Added

- ``examples/nested_lab.py`` — a ``GuestHypervisor.libvirt`` host carrying its own
  inner plan (an isolated DHCP/DNS switch + two inner VMs), asserting inner
  VM-to-VM reachability through ``orch.nested``. The first user-facing
  nested-virtualization example.
- ``examples/multi_tier_app.py`` — a NAT'd web tier and an air-gapped backend
  tier: a multi-homed ``web`` guest plus a ``db`` guest (carrying a ``HardDrive``
  data disk) on the isolated switch, asserting inter-VM reach and egress
  isolation.
- ``ESXiKickstartBuilder(allow_tcp_forwarding=…)`` — also threaded through
  ``GuestHypervisor.esxi`` — bakes ``AllowTcpForwarding yes`` into the installed
  ESXi node's sshd so its ``guest_gateway`` can SSH-jump to guests; the easy path
  for SSH jump-host testing (ESXI-22).

### Changed

- ESXi preflight (``esxi-uplink-pnic-missing``) now requires a free physical NIC
  only when a switch actually requests NAT egress, so a plan whose VMs need no
  egress at all no longer fails preflight (ESXI-21).
- A failed build's base64 log tail is decoded before it is printed: a block
  corrupted by interleaved chatter on the shared serial console no longer dumps a
  raw base64 blob to the console (BUILD-23).
- Driver docs: each backend's **Support level** section now states whether the
  backend is certified working in prose, replacing the per-capability table; the
  Proxmox page is corrected to reflect its certified status.

### Removed

- ``examples/pve_node.py`` — the Proxmox builder's live-cert vehicle. The Proxmox
  backend remains certified via the ``tests/plans/{generic,proxmox}`` corpus.

## [1.0.0] — 2026-06-09

First stable release: the public API is frozen and the driver layer ships
libvirt, Proxmox VE, and ESXi behind one backend-agnostic plan. Each driver's
support level is documented under **Support level** in `docs/user/drivers/`.

### Added

- ``examples/pve_node.py`` — stands up a Proxmox VE node as a libvirt guest
  via ``ProxmoxAnswerBuilder`` (installer-origin, UEFI/q35) and ``leak()``s
  it, the live-certification vehicle for the Proxmox builder *and* the host
  the Proxmox driver is certified against. The driver is now certified live
  end-to-end against such a node (``tests/plans/{generic,proxmox}`` green);
  findings in ``docs/dev/e2e-findings-proxmox.md``.
- ``testrange/guest_io.py`` — a neutral module of callable-shape
  ``Protocol``s (``GuestExec`` / ``GuestReadFile`` / ``GuestWriteFile``)
  plus a re-export of ``ExecResult``. These are exactly the shapes of
  ``Communicator.execute`` / ``read_file`` / ``write_file``; they let a
  builder's readiness hook and a native-agent communicator take loose
  callables without importing a Communicator or driver.
- ``NativeCommunicator`` — a Communicator backed by a hypervisor's native
  guest agent (QEMU Guest Agent on libvirt). Takes no Plan-time
  arguments; the orchestrator binds it with three VM-bound callables
  (``execute`` / ``read_file`` / ``write_file``) pulled off the driver.
  No SSH, no credentials, no IP discovery — reaches the guest in-band.
  The guest must have ``qemu-guest-agent`` installed (user-declared in
  the builder). See ``examples/native_agent.py``.
- ``HypervisorDriver.native_guest_execute`` / ``native_guest_read_file``
  / ``native_guest_write_file`` — optional-capability accessors
  returning VM-bound guest-agent callables; the default raises
  ``DriverError`` for backends with no native agent. ``LibvirtDriver``
  implements them via ``_LibvirtGuestAgent`` over
  ``libvirt_qemu.qemuAgentCommand``. Every libvirt domain now renders an
  ``org.qemu.guest_agent.0`` virtio channel unconditionally.
- ``GuestAgentError(DriverError)`` — raised when a native guest-agent
  command fails (agent not responding, timeout, QGA protocol error).
- VM snapshot lifecycle on the driver: ``create_snapshot`` /
  ``list_snapshots`` / ``delete_snapshot`` / ``restore_snapshot`` on the
  ``HypervisorDriver`` ABC and ``LibvirtDriver``, reachable from test code
  via ``orch.driver``. ``create_snapshot(..., mem=False)`` takes a
  disk-only snapshot; backends that don't support memory snapshots raise
  ``DriverError`` when ``mem=True``. See the snapshot recipe in the
  Running tests guide and ``examples/hello_world.py``
  (``snapshot_lifecycle``).
- ``testrange preflight <plan> --profile <name>`` — a read-only verb that
  connects to the bound backend, runs every preflight check, and prints each
  one with its result (ok / blocked / skipped) plus the discovered host
  capacity. Exits non-zero on a blocker; creates and destroys nothing.
- **Host-resource preflight gate.** ``HypervisorDriver.host_capacity() ->
  HostCapacity | None`` (implemented on libvirt, Proxmox, ESXi, and the mock)
  feeds a shared ``resource_findings`` check that rejects impossible plans
  before anything stands up — a VM larger than the host's RAM, an aggregate
  that cannot be co-resident, more vCPUs than the host has logical CPUs, or a
  pool larger than the backing store. The probe is best-effort: a backend that
  cannot introspect its host returns ``None`` and the gate is skipped rather
  than turned into a false blocker.
- ``PreflightCheck`` + ``PreflightReport.checks`` / ``from_checks`` /
  ``render_full`` — preflight now groups findings under named checks so the
  ``preflight`` verb can show what was checked, not just what blocked.
- **Scrollable dashboard panes.** The live Log and Serial panes scroll back
  through their ring buffers from the keyboard: Tab (or ←/→) switches the
  focused pane, ↑/↓ scroll a line, PgUp/PgDn a page, Home/``g`` jumps to the
  oldest line, End/``G`` snaps back to the live tail.

### Changed

- **Builder readiness is now callable-injection, not argv.**
  ``Builder.wait_ready_argv(spec, recipe) -> tuple[str, ...] | None`` is
  replaced by ``Builder.wait_ready(spec, recipe, execute: GuestExec) ->
  None``. The orchestrator hands the builder its VM's ``execute``
  callable; the builder runs its own readiness command, inspects the
  ``ExecResult``, and raises ``BuildNotReadyError`` itself.
  ``CloudInitBuilder`` runs ``cloud-init status --wait`` with an inline
  ``timeout=300.0``.
- **Removed ``Orchestrator(ready_timeout_s=...)``.** The readiness
  ``execute`` call now lives in the builder, which owns its own timeout
  inline — there is no framework-wide knob.
- **The live dashboard is now full-screen.** It runs on the terminal's
  alternate screen buffer, and the VMs + Tests top row takes a fifth of the
  height so the streaming Log and Serial panes get the rest. Because the
  alt-buffer is torn down on exit, ``run`` prints a one-line pass/fail tally
  (plus any failures) on the restored screen afterward.

### Fixed

- **libvirt: UEFI domains no longer enable Secure Boot.** ``_os_xml`` now
  emits ``<firmware><feature enabled='no' name='secure-boot'/></firmware>``
  for ``firmware="uefi"`` VMs. A TestRange UEFI VM boots a *captured*
  installer-built disk with *fresh* per-domain EFI vars via the
  removable-media fallback (``\EFI\BOOT\BOOTX64.EFI``), which a Secure-Boot
  OVMF rejects ("prohibited by secure boot policy") — so the run-phase boot
  never came up. Signed images still boot. Surfaced certifying the Proxmox
  builder (PVE-57).
- **Proxmox: QGA file-read/exec no longer corrupt binary content.** PVE's
  ``agent/file-read`` (and exec out/err-data) surface the guest's raw bytes
  as a latin-1 string; the driver re-encoded that with utf-8, doubling every
  ``0x80``–``0xFF`` byte (a 256 KiB binary read came back 393216 bytes). It now
  recovers bytes with a latin-1 encode, and fails loud on a ``truncated``
  ``file-read`` instead of silently returning a head (PVE-58).
- **Proxmox: memory-snapshot rollback/delete retry the transient config
  flock.** A ``mem=True`` snapshot rollback/delete holds
  ``/var/lock/qemu-server/lock-<vmid>.conf`` past the API task (the QEMU
  vmstate save/resume), so a follow-on op failed with ``can't lock file …
  got timeout``. ``restore_snapshot``/``delete_snapshot`` now retry that
  transient lock (same shape as the post-import resize retry) (PVE-58).
- **The live dashboard no longer flickers on VTE-based terminals** (e.g.
  Terminator on Debian 13). The in-band cursor-up redraw is replaced by the
  alternate screen buffer's controlled full-screen repaint (CORE-86).

## [0.2.0] — 2026-05-14

Post-0.1.0 work: an interactive REPL at test-execution phase, full
static-IP support on the Plan side with plan-wide addressing validation,
a multi-NIC reliability fix for the run-phase netplan that switches
matching from interface-name globs to stable MACs, and a
builder-declared run-phase readiness hook brokered by the orchestrator.

### Added

- **Builder-declared run-phase readiness, brokered by the orchestrator.**
  ``Builder.wait_ready_argv(spec, recipe) -> tuple[str, ...] | None`` on
  the ABC (non-abstract, default ``None`` — no check). ``CloudInitBuilder``
  overrides it to return ``("cloud-init", "status", "--wait")``. The
  orchestrator runs the check against each VM's bound communicator after
  ``_bind_communicators`` and before yielding the ``OrchestratorHandle``,
  raising ``BuildNotReadyError`` on a non-zero exit. Readiness is no
  longer something a plan author wires into ``TESTS`` by hand; the
  ``cloud_init_finished`` test was dropped from the shipped examples.
- ``Orchestrator(ready_timeout_s=...)`` — per-VM timeout for the builder
  readiness check; defaults to 300s.
- ``BuildNotReadyError(BuilderError)`` — raised when a brought-up VM
  never reaches the builder-declared ready state.

- ``testrange repl <plan>`` — brings the range up exactly the way ``run``
  does (preflight → install → run-phase → communicators bound), prints
  the ``describe`` output, then drops into a stdlib ``code.interact()``
  session with ``orch``, ``plan``, and ``tests`` pre-bound. Ctrl-D /
  ``exit()`` triggers normal teardown. Same exit codes as ``run``
  (0/1/2/130). Inside the REPL — or from test code — ``orch.leak()``
  skips teardown so the user can SSH in to debug; ``testrange cleanup
  <run_id>`` tears down later.
- ``NetworkIface.ipv4`` (and ``LibvirtNetworkIface.ipv4`` by inheritance)
  for static-IP NICs. DHCP remains the default. Install still runs on a
  transient DHCP-only subnet so apt works; the static address is applied
  at run-phase via a netplan staged into cloud-init ``write_files`` plus
  a ``99-testrange-disable-network.cfg`` drop-in so cloud-init doesn't
  re-render later boots. No ``netplan apply`` mid-install — the cached
  post-install disk already contains the static-aware netplan, and the
  run-phase boot reads it directly.
- ``testrange/networks/validate.py`` — plan-wide addressing validation
  that accumulates every problem into one ``ValueError`` at Hypervisor
  construction: CIDR membership, gateway/network/broadcast collision,
  DHCP-pool collision, duplicate ``ipv4`` within a network, and
  ``Network(dhcp=False)`` + NIC without ``ipv4``.
- ``NetworkAddressing`` — ``(cidr, prefix_len, gateway, dhcp)`` view per
  network. Builders take a ``Mapping[network_name, NetworkAddressing]``
  rather than the whole hypervisor, keeping the builder stovepipe
  hypervisor-agnostic.
- ``Builder.render_seed`` / ``Builder.config_hash`` now take a
  ``macs: Sequence[str] = ()`` kwarg. The orchestrator computes stable
  MACs via ``driver.compose_mac(plan_name, vm_name, nic_idx)`` and
  threads them in.
- ``examples/private_public.py`` — airgap-vs-internet topology with a
  dual-homed client. Two switches (one ``internet=False``), three VMs
  (two webservers, one client with NICs on both networks), and
  reachability tests asserting the isolation as well as the install-phase
  internet access that builds the air-gapped server's nginx.
- ``RESEARCH.md`` — open design notes (first entry: ESXi DHCP-sidecar
  strategy). Distinct from ``PLAN.md`` (agreed design) and ``TODO.md``
  (work queue).
- ``docs/user/writing-a-plan.md`` networking section — ``ipv4=`` usage,
  the first-NIC rule for network-using communicators, DHCP vs static
  trade-offs.

### Changed

- ``OrchestratorHandle`` gains a ``leak: Callable[[], None]`` field
  bound to ``Orchestrator.leak``. Available from both test code and the
  REPL.
- ``Orchestrator._discover_ip`` short-circuits to the first NIC's
  ``ipv4`` when set; falls through to the existing DHCP-lease poll
  otherwise.
- ``HypervisorDriver.preflight`` gains an ``install_network=`` kwarg so
  the orchestrator brokers the transient install CIDR down to the
  driver, which folds it into the pairwise overlap check with a
  ``fix_hint`` on collision.
- ``CloudInitBuilder.render_user_data`` / ``render_seed`` /
  ``config_hash`` accept ``macs: Sequence[str] = ()``. When provided,
  the run-phase netplan matches NICs by MAC. The cache key folds in
  MACs via the rendered seed text; stable MACs → stable cache hits.
- ``testrange describe`` NIC line now shows ``ipv4=<addr>`` or ``dhcp``
  so plan addressing is visible at a glance.

### Fixed

- **Multi-NIC run-phase netplan matched by broken name globs.**
  ``_render_run_netplan_yaml`` previously generated
  ``match: {name: en*}`` for NIC0 and ``match: {name: en{idx}*}`` for
  NIC1+. On guests with systemd's predictable interface naming
  (``enp1s0``, ``enp2s0``, …), ``en1*`` matches nothing — ``enp2s0``
  starts with ``enp``, not ``en1``. NIC1+ silently went unconfigured,
  surfacing later as DHCP failure / no route on the second network.
  Now matches by stable MAC when the orchestrator supplies them; the
  legacy name-glob fallback is kept for callers without MACs and is
  only safe for single-NIC VMs. The "skip run-phase netplan entirely"
  short-circuit now fires only for single-NIC all-DHCP VMs; multi-NIC
  always emits the run-phase netplan.
- ``cache-server`` first-boot permission error: nginx (uid 101 inside
  the container) couldn't write to ``./storage`` because the host-side
  mount is owned by the host user. A one-shot ``cache-init`` sidecar
  (alpine + ``chown -R 101:101``) runs to completion before nginx
  starts (``service_completed_successfully`` gate). Re-runs are
  idempotent.

## [0.1.0] — 2026-05-12

HTTP cache tier — an optional second tier behind the local
content-addressed store, served by a dumb nginx WebDAV server (see
``cache-server/``). On a local miss the broker falls through to HTTP
and materializes into local on a hit; on a local write it mirrors back.
Local stays the source of truth; HTTP failures log a warning and never
abort the local op.

### Added

- ``testrange/cache/http.py`` — ``HttpCache``: ``resolve`` / ``fetch`` /
  ``push`` / ``delete`` / ``add_name`` / ``forget_name`` over
  ``/isos/<sha>.{bin,json}`` + ``/names/<n>``. Write order is bin →
  sidecar → names so a half-uploaded entry stays invisible; delete is
  the inverse. TLS is never verified — the server is expected to sit
  behind a private network gate.
- ``testrange/cache/_names.py`` — shared name validator
  (``[A-Za-z0-9._-]{1,255}``). Prevents path-traversal on
  ``/names/<n>`` and tightens the local tier to match.
- Global ``--cache <URL>`` CLI flag. No env var — every invocation is
  self-describing.
- New ``cache push`` / ``cache pull`` subcommands for manual
  reconciliation when the HTTP tier was unreachable during the original
  add or you want to warm local before going offline.
- ``[http]`` install extra pulling ``requests>=2.31``.
- ``cache-server/README.md`` — quickstart, cert generation, storage
  layout, security caveats.
- 39 new unit tests covering wire-level ``HttpCache`` behavior, broker
  policy (fallthrough, mirror, mirror-failure-tolerance), and CLI
  push/pull paths.

### Changed

- ``CacheManager`` refactored from a thin wrapper into a real broker.
  ``resolve(ref, fetch=True)`` (default) materializes from HTTP into
  local on a hit; ``fetch=False`` returns the HTTP info without
  downloading and is what passive callers (``describe``, preflight)
  use.
- ``CacheEntryInfo.path`` widened to ``Path | None`` — HTTP-tier
  entries resolved without ``fetch`` carry no local path.
- ``testrange cache add/del/rename/forget-name`` now route through the
  broker so each operation mirrors to HTTP when configured.
- Orchestrator install-phase ``resolve`` goes through the broker
  (HTTP fallthrough); post-install snapshot mirrors to HTTP via
  ``cache.add`` so multi-host setups share the cooked disks.
- Libvirt driver preflight ``resolve`` switched to ``fetch=False`` so
  cache findings don't pull a multi-GB base image just to print a
  checklist line.

## [0.0.1] — 2026-05-11

First tagged snapshot. Phases 0–6 brought v0 to feature completeness;
this tag also includes the post-v0 follow-ups that make the example
plan run end-to-end against a real ``qemu:///system`` libvirtd.

### qemu:///system bring-up + SSHKey reshape (2026-05-11)

Post-Phase-6 fixes from running ``examples/hello_world.py`` against a
real libvirtd. System-mode runs qemu as the ``libvirt-qemu`` service
account, so the orchestrator UID can't write to ``/var/lib/libvirt/
images`` or read the files inside it — everything needs to go through
the libvirt stream API.

- ``LibvirtDriver`` ``pool_root`` default is URI-aware:
  ``/var/lib/libvirt/images/testrange`` for any URI ending in
  ``/system`` (local and ``qemu+ssh://.../system``);
  ``~/.local/share/testrange/pools`` for ``/session``.
- Volume traffic now flows through libvirt streams in both directions.
  ``upload_to_pool`` (idempotent) and ``write_to_pool`` (replace-if-
  exists, raw format for cloud-init seed bytes) use
  ``vol.upload(stream)`` + ``stream.sendAll``. ``download_from_pool``
  uses ``vol.download(stream, 0, 0, 0)`` + ``stream.recvAll``.
  ``_stream_upload_to_vol`` is the shared inner loop.
- ``download_from_pool`` flattens first: clones into a no-
  ``<backingStore>`` qcow2 inside the pool via ``createXMLFrom``
  (which under the dir-pool driver invokes ``qemu-img convert`` and
  reads through the backing chain), streams the clone back, deletes
  the clone in a ``finally``. Cached qcow2 are now self-contained
  instead of carrying a baked-in ``backing_file`` pointer to the
  previous run's pool path.
- ``create_pool`` no longer mkdirs the target directory from Python;
  ``sp.build(0)`` owns directory creation under libvirtd's user.
  Preflight skips the user-side mkdir attempt on ``/system`` URIs.
- ``destroy_pool`` now calls ``sp.delete(0)`` between ``destroy()``
  and ``undefine()`` so the per-run target directory is removed too.
  The LIFO state walker deletes contained volumes first.
- Every VM gets ``<graphics type='vnc' port='-1' autoport='yes'
  listen='127.0.0.1'/>`` + ``<video><model type='virtio'/></video>``.
  VNC + virtio-gpu are universally compiled into modern qemu; SPICE
  and QXL are commonly stripped from distro builds. ``virt-viewer
  <domain>`` connects via the autoport VNC.
- ``Store.record_intent(..., **metadata)`` stamps initial metadata
  at intent time, so a backend create() failure between intent and
  confirm still leaves enough info for the ``destroy()`` dispatcher
  to route correctly. Orchestrator passes ``pool_backend=`` for all
  volume kinds (``base_image``, ``install_disk``, ``install_seed``,
  ``run_disk``).
- ``_teardown`` logs LIFO per-resource progress, an "X ok, Y failed"
  summary, and a tail warning if anything is left in state.
- New ``_ensure_base_in_pool`` helper funnels both install and run
  phases through a single in-pool base image (content-addressed by
  cache stem), recorded with intent-then-confirm. ``_install_one_vm``
  ingests the post-install disk through a user-side temp file
  (``NamedTemporaryFile`` under ``/tmp``) since libvirt-qemu owns
  the pool file.

### SSHKey reshape + deterministic generation (2026-05-11)

- ``SSHKey`` now has three fields: ``pub`` (PEM
  ``SubjectPublicKeyInfo``), ``priv`` (OpenSSH PEM, unencrypted),
  and ``auth_line`` (single-line ``ssh-ed25519 AAA... comment``
  for ``authorized_keys``).
- ``gen_ssh_key(comment=...)`` derives the 32-byte Ed25519 seed from
  ``sha256(comment)``. Same comment yields the same keypair across
  runs. **Insecure by design** — only safe for ephemeral, isolated
  test VMs. Makes the rendered cloud-init seed byte-stable, which
  is what ``config_hash`` hashes over, which is what lets the
  post-install cache hit on subsequent runs.
- ``_load_private_key`` resolves paramiko key classes lazily via
  ``getattr(paramiko_mod, name, None)`` instead of in a tuple
  literal — paramiko 4.x dropped ``DSSKey``, which previously
  AttributeError'd at the tuple-construction site.

### Phase 6 — Polish, signal handling, docs (2026-05-11)

CLI flag wiring, SIGTERM/SIGHUP cleanup, README + user guides +
architecture overview + initial ADR set. v0 complete.

- ``testrange run --fail-fast``: stop on first test failure.
- ``testrange run --leak-on-failure``: if any test fails, skip
  teardown so the user can SSH in to debug; ``testrange cleanup
  <run_id>`` tears down later.
- ``Orchestrator.leak()`` semantics tightened: now skips teardown
  unconditionally (no longer requires an exception). Aligns with
  the CLI flag and with live-debugging use.
- ``Orchestrator.__enter__`` installs SIGTERM + SIGHUP handlers that
  raise ``KeyboardInterrupt``, routing through ``__exit__``'s
  cleanup path. CTRL-C already worked (Python's default behavior);
  this completes the picture for ``systemd`` and shell timeouts.
- Handlers restored on ``__exit__`` so the orchestrator doesn't
  leak global state into surrounding code.
- README quickstart, ``docs/user/install.md``,
  ``docs/user/writing-a-plan.md``, ``docs/Architecture-and-Design.md``,
  ``docs/dev/contributing.md``.
- ADR-0001 (subprocess ban), 0002 (no asyncio), 0003 (state schema
  v1), 0004 (CacheEntry only), 0005 (OSDrive distinct), 0006
  (driver-level stable MAC).
- One new CLI test for the new flags. Total: 211 passed; ruff +
  mypy --strict clean.

### Phase 5 — SSH communicator + test runner (2026-05-11)

Test code can now talk to brought-up VMs. ``run_tests(tests, plan)``
executes the user's test functions sequentially against an
``OrchestratorHandle`` whose VMs have bound SSH communicators with
discovered IPs.

- ``SSHCommunicator`` real implementation: paramiko-backed, lazy
  connect with retry loop, ``execute(argv, *, timeout, cwd)`` via
  shlex-joined command, ``read_file`` / ``write_file`` via SFTP.
  Auth precedence pkey-if-present-else-password (PLAN.md decision 7).
  Tries Ed25519 / RSA / ECDSA / DSS when loading private keys.
- ``HypervisorDriver.get_lease_ip(network_backend, mac)`` on the ABC;
  LibvirtDriver walks ``network.DHCPLeases()`` and matches by MAC.
- ``Orchestrator._bind_communicators`` runs after the run phase,
  discovers each VM's IP via ``get_lease_ip`` keyed on the stable
  MAC, and dispatches by communicator type to call its per-type
  ``bind()`` with the right inputs.
- ``run_tests(tests, plan, *, fail_fast=False)`` actually executes
  tests now. Continue-on-failure default; ``fail_fast=True`` stops
  on the first failure. Tracebacks captured into ``TestResult.error``.
- 27 new unit tests (paramiko mocked end-to-end, test runner,
  communicator bind during enter). Total: 210 passed; ruff +
  mypy --strict clean.

### Phase 4 — Orchestrator install + run phases (2026-05-11)

End-to-end bring-up + teardown. ``with Orchestrator(plan) as orch:``
takes the plan through preflight, install (cache-aware, builder-driven),
run, and cleanup. ``testrange run plan.py`` is wired but executes a
test-runner placeholder until Phase 5.

- ``testrange.orchestrator.Orchestrator``: context manager that drives
  the full phase sequence. Driver is inferred from the hypervisor type
  (LibvirtHypervisor -> LibvirtDriver). State recorded BEFORE each
  backend create-call (PLAN.md decision 4); state dir cleaned up after
  successful teardown.
- **Install phase**: per-VM `config_hash` lookup against the local
  cache; cache hit skips the install VM build entirely. Cache miss
  brings up a transient install VM on a transient internet-NAT network
  with the cloud-init seed attached, polls driver power-state until
  the VM self-terminates via `runcmd: [..., poweroff]`, then ingests
  the post-install disk into the cache via `LocalCache.add`. Install
  resources are recorded in state.json then forgotten as they're
  cleaned up.
- **Run phase**: user networks created, run VM gets a fresh overlay
  off the cached post-install disk, defined + started with no seed.
- **Teardown**: LIFO over state.json resources, tolerates per-resource
  failures, removes the state dir on a clean run.
- ``Plan(name="hello")`` kwarg for naming a plan (used in stable-MAC
  derivation and state.json).
- ``InstallTimeoutError`` / ``OrchestratorError`` exception types.
- ``HypervisorDriver.destroy(kind, name, **metadata)`` now accepts
  metadata to route volume-kind cleanups to the right pool.
- ``run_tests(tests, plan)`` enters the orchestrator and returns a list
  of placeholder ``TestResult``s — Phase 5 will replace with real
  execution.
- CLI: ``testrange run plan.py`` brings up + tears down. Exit codes:
  0 ok, 1 failure, 2 preflight failure.
- ``testrange.orchestrator`` re-exports ``Orchestrator``.
- 10 new unit tests using a fully-mocked driver to exercise the entire
  lifecycle without libvirt. Total: 192 passed; ruff + mypy --strict
  clean.

### Phase 3 — VM CRUD + CloudInitBuilder seed (2026-05-11)

VM/volume primitives on the libvirt driver, full cloud-init seed
rendering (user-data + meta-data + network-config) into a pycdlib-built
``cidata`` ISO, and a deterministic ``config_hash`` ready for the
two-phase install cache key.

- `HypervisorDriver` ABC: added VM CRUD (`create_vm`, `start_vm`,
  `shutdown_vm` with graceful→force escalation, `destroy_vm`,
  `get_vm_power_state`) and volume ops (`write_to_pool`,
  `create_overlay_disk`, `delete_volume`).
- `LibvirtDriver`: domain XML rendering for spec→libvirt (CPU, memory,
  qcow2 OS disk, optional seed CD-ROM, NICs with stable MAC + driver
  model). Overlay volumes via libvirt's `<backingStore>` (no
  `qemu-img` subprocess). `shutdown_vm` polls power state with a
  configurable timeout, escalates to destroy on timeout.
- `destroy(kind, name)` now routes vm/install_vm and
  network/install_network kinds.
- `CloudInitBuilder.render_seed`: builds an ISO9660+Joliet+RockRidge
  seed image labeled ``cidata`` with three files. user-data is
  ``#cloud-config`` YAML (PyYAML); always includes a self-terminating
  ``poweroff`` at the end of runcmd so install VMs power off on
  completion. Apt + Pip packages, post_install_commands, sudo, SSH
  pubkeys, plaintext passwords (via chpasswd) all plumbed.
- `network-config` matches interfaces by **name**, not MAC — sidesteps
  the MAC-baked-into-cached-disk failure mode independent of the
  driver's stable-MAC work.
- `CloudInitBuilder.config_hash(spec, recipe, *, base_sha)`:
  deterministic 16-char hex of rendered user-data + meta-data +
  network-config + the resolved base disk sha. Pure, no I/O, no
  run_id.
- 21 new unit tests (cloud-init render + ISO read-back + VM/volume
  XML rendering + dispatch). Total: 183 passed; ruff + mypy --strict
  clean.

### Phase 2 — Libvirt driver foundation + state machinery (2026-05-11)

`HypervisorDriver` ABC, `LibvirtDriver` lazy-imported runtime (preflight
+ network/pool CRUD), state machinery (`state.json` + `state.pid`),
PID-checked `testrange cleanup`. Libvirt integration tests skip when
`libvirt-python` isn't installed.

- `testrange.drivers.base.HypervisorDriver` ABC: `connect`, `disconnect`,
  `preflight`, `compose_resource_name`, `compose_mac`, network+pool
  CRUD, `destroy(kind, name)` dispatch.
- `testrange.drivers.libvirt.LibvirtDriver`:
  - `compose_resource_name` produces deterministic libvirt-safe names.
  - `compose_mac` derives stable per-NIC MACs under the KVM OUI
    (`52:54:00:…`) — a driver concern, not shared utility.
  - `preflight` collects cache-resolvability + subnet-overlap +
    pool-writable findings (read-only, side-effect-free invariant
    intact apart from a `mkdir` of the pool root).
  - Network/pool CRUD via libvirt XML rendering + libvirt-python.
  - `libvirt` import is lazy so the package is usable on hosts without
    libvirt-dev installed; integration tests behind `-m libvirt`.
- `testrange.preflight`: `PreflightFinding` / `PreflightReport` with
  error/warning severities and `render()`.
- `testrange.state`:
  - `Resource` (kind, backend_name, plan_name, intent_at/outcome_at,
    metadata dict) — schema-flexible per PLAN.md decision 4.
  - `State` envelope, schema-version 1.
  - `StateStore`: atomic `state.json` writes (.partial + os.replace);
    sibling `state.pid` file with `is_pid_alive()` check.
    `require_dead()` raises `StateLockedError` if the owning PID is
    alive — replaces FileLock per PLAN.md decision 16.
  - `cleanup_run` / `cleanup_all` walk resources in reverse, dispatch
    `driver.destroy(kind, backend_name)`, tolerate per-resource
    failures, leave state in a self-consistent state.
- CLI: `testrange cleanup <run-id>`, `--all`, `--dry-run`. Exit codes:
  0 ok, 1 PID-locked, 2 missing/bad state, 3 partial-failure.
- 59 new unit tests + 2 integration tests (skipped here). Total: 162
  passed; ruff + mypy --strict clean.

### Phase 1 — Cache layer + cache CLI (2026-05-11)

Local content-addressed cache works end-to-end. URLs and filepaths drop
from Plan-time entirely — disks are referenced by ``CacheEntry``.

- `testrange.cache.LocalCache`: file-backed cache rooted under
  `$XDG_CACHE_HOME/testrange/isos/`, with `<sha>.bin` + sidecar
  `<sha>.json` layout. Atomic writes via `.partial` + `os.replace`.
- `add(path_or_url)` ingests local files or http(s):// URLs (urllib;
  no new deps). Same-content adds dedupe by sha and add to the `names[]`
  alias list.
- `resolve(identifier)` accepts a full sha, a sha-prefix (≥ 16 hex
  chars), or a pretty-name; raises `CacheMissError` cleanly on miss.
- `add_name` / `forget_name` / `delete` round-trip aliases and entries.
- `testrange.cache.CacheManager`: wraps `LocalCache`; `attach_http(url)`
  hook for the future HTTP tier (validation only in v0).
- CLI: `cache add | list | del | rename | forget-name`. `cache list`
  prints a width-aligned table with sha / size / names / origin.
- `testrange describe` now resolves every `CacheEntry` against the
  local cache and shows either the short-sha + size (hit) or a
  `⚠ not in cache` warning (miss).
- 28 new unit tests (cache layer + CLI subcommands). Total: 103 passed.
- ruff: clean. mypy --strict: clean.

### Phase 0 — Skeleton & Plan-time data types (2026-05-11)

Foundation work — package builds, lints clean, unit tests pass, no runtime
yet. Plan files import and pretty-print via `testrange describe`.

- Project scaffolding: `pyproject.toml`, `.gitignore`, `README.md`,
  `CHANGELOG.md`, dev/integration test layout, ruff + mypy --strict +
  pytest config.
- Subprocess ban enforced two ways: ruff `flake8-tidy-imports` banned-api
  rule, plus `tests/unit/test_subprocess_ban.py` as CI safety net.
- `testrange/` package: `_log.py` (stdlib logging with run-id adapter),
  `exceptions.py` (typed error hierarchy), `cli.py` (argparse skeleton
  with `describe` implemented + `cache`/`run`/`cleanup` stubs).
- Plan-time data types:
  - `Plan(*hypervisors)` (variadic; v0 enforces exactly one).
  - `LibvirtHypervisor(connection=, networks=, pools=, vms=)` with
    cross-reference validation (NIC→Network, OSDrive→Pool, name
    uniqueness).
  - `VMSpec` with singleton-device runtime checks (exactly one CPU,
    Memory, OSDrive).
  - `VMRecipe(spec=, builder=, communicator=)`.
  - `VMHandle` runtime view (Phase 5 fills it).
  - Devices: `CPU`, `Memory`, `OSDrive`, `HardDrive`, `NetworkIface` ABC,
    `LibvirtNetworkIface(driver=)`, `StoragePool`.
  - Networks: `Network(name, cidr, dhcp=, dns=)`, `Switch(name, *nets,
    mgmt=, internet=)`.
  - Credentials: `Credential` ABC, `PosixCred`, `gen_ssh_key` (in-memory
    Ed25519 keypair via `cryptography`; never touches disk).
  - Builders: `Builder` ABC, `CloudInitBuilder(base=, credentials=,
    packages=, post_install_commands=)` (data-only; rendering in Phase 3).
  - Communicators: `Communicator` ABC, `ExecResult` dataclass,
    `SSHCommunicator(username)` (Plan-time skeleton with single-use
    bind guard; `execute` etc. raise NotImplementedError until Phase 5).
  - Cache: `CacheEntry(identifier)` Plan-time data type with sha vs
    pretty-name auto-detection.
  - Packages: `Package` ABC, `Apt`, `Pip`.
- `examples/hello_world.py` matching the target API from `PLAN.md`.
- `testrange describe` prints a structured topology summary including
  CacheEntry references (with Phase 1 resolution warning).
- 75 unit tests; ruff + mypy --strict pass.
