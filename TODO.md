# TODO

Convention: items don't get deleted. When something is done or
superseded, it moves to the **Done / Superseded** section at the bottom
with a date stamp.

## Short-term

- **`addr` redesign doc fallout (opened 2026-05-21).** The `NetworkIface.addr`
  redesign removed the `ipv4=` kwarg and the "DHCP-by-default" behavior, so the
  user docs that teach the old model need a rewrite to `addr=` /
  `DHCPAddr()` / `StaticAddr(...)`:
  - `docs/user/writing-a-plan.md:120-136` — "Each NIC is DHCP by default. Pin a
    static address with `ipv4=`" + the `ipv4` validation bullets. Default is now
    `addr=None` (unconfigured); DHCP is explicit `DHCPAddr()`.
  - `docs/user/drivers/networking-modes.md:38,203,214` — the `ipv4="..."` static
    column, the "NIC without `ipv4` … is rejected" line (now allowed; it's
    unconfigured), and the "you must set `ipv4=` on every NIC" note.
  - Cover the new capabilities: `StaticAddr` gw/dns dictation (unmanaged
    gateway), and `SSHCommunicator(nic_idx=)` for multi-NIC VMs.
- **Live libvirt smoke test for the `addr` redesign still unrun.**
  `python -m testrange.cli run examples/hello_world.py` provisions real VMs;
  verify the static + DHCP render paths on a real guest before considering the
  redesign fully closed. (Unit suite + ruff + mypy already green.)

The `.tours/code-review.tour` and `.tours/fixlist.tour` items were all
applied on 2026-05-19 (see Done / Superseded). The `.tours/*` files are
the user's; left in place for them to clear. Two follow-ups remain open:

- **Sidecar image versioning (CR-TOOLS-1, was a question).**
  `tools/build-sidecar-image/build.sh` produces an unstamped
  `testrange-sidecar.qcow2` with unpinned Alpine packages; the
  post-install cache hash folds the rendered seed text, not the sidecar
  image's hash, so a drifted sidecar silently invalidates nothing. A
  SHA-stamped, version-tracked sidecar artifact is the fix — a build-tooling
  change, deferred as its own task rather than rolled into this sweep.

### Audit 2026-05-19 — architecture + docs review (driver layer excluded)

Findings from a full read-only review of examples/tests/non-driver code
and all prose/docstrings. The architecture itself was judged solid
(stovepipe discipline enforced structurally via `guest_io` callables +
`RunContext` broker; coherent error hierarchy; record-before-create
ledger). One real bug, plus polish, test gaps, and substantial doc drift
(most of it introduced by today's API changes + the orchestrator split).

**Correctness — fix first**

- ~~**[BUG] Run-phase netplan renders `dhcp4: true` for a no-DHCP NIC.**~~
  **Resolved 2026-05-21** by the `NetworkIface.addr` redesign (the initial
  "string arg" sketch became a typed sum type). Full entry under
  Done / Superseded.

**Architecture / cleanup (non-driver)**

- `testrange/networks/validate.py:101-106` hardcodes `network_address + 100`
  / `+ 254` in the DHCP-pool error instead of `USER_STATIC_LO`/`USER_STATIC_HI`
  from `_addressing_consts.py` — the exact drift those constants exist to
  prevent. Import and interpolate them.
- Inconsistent hypervisor accessor: `run_phase._switch_for_network`
  (`run_phase.py:171`) uses `ctx.plan.hypervisor.all_switches`, but
  `runtime._all_switches` (`runtime.py:121,137`) and `install_phase.py:51`
  use `getattr(hyp, "networks", None)`. Pick the typed `all_switches`/
  `all_networks` accessor everywhere and drop the `getattr` defensiveness
  against an internal, typed value object (also `getattr(hyp,
  "install_uplink", None)`). `cli.py`'s `getattr` is defensible (prints an
  `Any`-typed plan entry); internal call sites are not.
- `testrange/plan.py:14-19`: the dataclass field defaults
  (`name: str = ""`, `field(default_factory=tuple)`) are dead — the
  hand-written `__init__` overrides construction and raises on empty name.
  Misleading (implies `Plan()` nameless is constructible). Drop the
  defaults or comment why they're inert.
- `testrange/orchestrator/runtime.py:201`: initialize
  `self._prior_signal_handlers = {}` in `__init__` rather than relying on
  the `getattr(self, ..., {})` guard in `_restore_signal_handlers`.
- `testrange/orchestrator/run_phase.py:157` catches `GuestAgentError` (a
  `DriverError` subclass) in the orchestrator layer — correct, but it's the
  one spot driver-error vocabulary legitimately crosses the stovepipe; add
  a one-line comment so it isn't "fixed" later.

**Tests**

- **Dead integration test.** `tests/integration/test_libvirt_driver.py:50-51`
  uses removed API: `Network("netA", "192.0.2.0/24", dhcp=True, dns=True)`
  (Network takes only `name` now) and `Switch(..., internet=False)`
  (`internet` is gone; it's `nat`). Raises `TypeError`/`TypeError` on any
  libvirt host. Rewrite to current API, or delete in favor of
  example-driven integration (`test_libvirt_qga.py` is the current model).
  Also a duplicate `import libvirt` at lines 18+20. *(Question: update or
  delete?)*
- **Tautology assertions.** `tests/unit/test_orchestrator.py:641,666,735`
  `assert "get_lease_ip" not in names` can never fail (method removed) —
  they protect nothing. Replace with the positive contract: static-IP case
  asserts `native_guest_read_file` was NOT called with `LEASEFILE`; rename
  `test_static_ip_skips_get_lease_ip` → `..._skips_lease_lookup`.
- **Missing tests for today's changes:**
  - `PosixCred.groups` (renamed from `extra_groups`): assert the field and
    the cloud-init render (`cloudinit.py:180-182` — sudo+empty → `["sudo"]`;
    bare groups pass through). Zero coverage currently.
  - ~~No-DHCP-no-static NIC netplan render (`test_staged_netplan_nic_on_bare_switch`)~~
    **(DONE 2026-05-21)** — landed with the `addr` redesign in
    `TestRunPhaseNetplanTriState`, alongside the `DHCPAddr`, static-derive, and
    static-dictate render cases and the `StaticAddr` value-object tests.
  - DHCP-discovery timeout path: non-matching lease + tiny `lease_timeout_s`
    asserting `OrchestratorError("did not acquire a DHCP lease")`.
  - Confirm `parse_dnsmasq_leases` edge branches (`<3` fields, missing
    `:`/`.`) are covered in `test_sidecar.py`; add if not.
- `tests/unit/test_orchestrator.py` `_FakeDriver.compose_mac` uses
  `abs(hash(vm_name))` — `str.__hash__` is PYTHONHASHSEED-salted, so the
  auto-registered lease IP varies per process; flaky if a future test
  relies on it. Switch to `hashlib`. (Introduced with today's sidecar-lease
  fake.)
- `_FakeDriver.destroy` (`test_orchestrator.py:242-260`) hand-reimplements
  the ABC's concrete `destroy()` dispatch and omits the `base_image`/`volume`
  missing-`pool_backend` guard (`drivers/base.py:339-343`); have the fake
  inherit the ABC's concrete `destroy()` so it can't drift.

**Examples**

- `examples/network_modes.py:2-3` has two explanatory comments — violates
  the no-comments-in-examples rule (the other three examples are clean).
  Move the prose to `docs/user/` and trim the docstring to one line.

**Docs — stale API (introduced today / by the orchestrator split)**

- `README.md:35` `Network("netA", "10.0.1.0/24")` → `Switch("sw1",
  Network("netA"), cidr="10.0.1.0/24")`; and `README.md:32-45` `Plan(...)`
  is missing the now-required `name=`.
- `docs/dev/architecture.md:50-55` still describes `get_lease_ip` /
  `DHCPLeases()` as the driver's DHCP-lease-lookup duty — **removed today**;
  delete the clause (lease is read from the sidecar over QGA). *(This is the
  clause I added earlier today; it's now fully stale.)*
- `docs/dev/extending/drivers.md:39` lists `get_lease_ip` as a method a new
  driver must implement → replace with the `native_guest_{execute,read_file,
  write_file}` trio.
- `docs/user/writing-a-plan.md:204` "polls the driver for the lease" →
  reads the sidecar's dnsmasq lease file over the guest agent.
- **Superseded by the `addr` redesign (2026-05-21)** — folded into the
  "`addr` redesign doc fallout" item below. `docs/user/drivers/networking-modes.md`
  and `docs/user/writing-a-plan.md` document the entire old `ipv4=` /
  "DHCP-by-default" model, which no longer exists.
- `docs/user/running-tests.md:144` suggests `virsh domifaddr` to find a
  leaked VM's IP — won't show sidecar-served leases; point at the sidecar
  lease file / the logged bind IP.
- **Stale orchestrator method names** (now module-level functions after the
  split): `_provision_switch`/`_materialize_sidecar_for`/`_bind_communicators`/
  `_discover_ip`/`_lookup_credential` appear in `docs/dev/architecture.md:99-108`,
  `docs/dev/extending/communicators.md:65-100`, and the `builders/base.py:90`
  docstring ("after `_bind_communicators`"). They live in
  `orchestrator/run_phase.py` (`bind_communicators`, `discover_ip`,
  `lookup_credential`) and `orchestrator/provision.py` (`provision_switch`,
  `materialize_sidecar_for`). Also `docs/dev/extending/communicators.md`
  should note `close()` re-bindability is transport-specific (SSH reconnects;
  QGA is terminal / `CommunicatorClosedError`).

**Docs — PLAN.md drift**

- *Fixed 2026-05-21:* §5 QGA `bind` (now three callables) + `SSHCommunicator`
  `nic_idx`; `--verbose` removed; `gen_ssh_key`→`SSHKey.generate` and the
  `SSHKey` import source; added §10 "NIC addressing" for the `addr` sum type;
  flagship example NIC given `addr=DHCPAddr()`.
- **Still open:** `PLAN.md` file-layout tree (§ "File layout (v0)") is stale —
  no `*/generic.py`, no `networks/libvirt.py`, no `orchestrator/phases.py`;
  missing `utils/`, `guest_io.py`, `preflight.py`, `_names.py`,
  `communicators/qga.py`, the drivers registry. Regenerate from the real tree.

**Docs — TODO.md self-drift (Long-term section)**

- `Switch(internet=True, uplink=...)` → `Switch(nat=True, uplink=...)`
  (the `internet` flag is gone).
- "mgmt ... gets a single `.3/<prefix>` adapter" → mgmt is `.2`
  (`MGMT_OFFSET=2`).
- "See `Orchestrator._provision_bridge`" → no such method; bridge/switch
  provisioning is `orchestrator/provision.py:provision_switch`.
- `Switch(router=True)` note claims static netplans currently get no default
  route (`NetworkAddressing.default_route` "removed") — misleading;
  `NetworkAddressing.gateway` already drives the static default route
  (`cloudinit.py:485-487`).

**Verified clean (no action)**

- Generic-prose-stays-generic and no-cross-stovepipe-in-class-docstrings
  conventions: clean across the non-driver docstrings.
- ADR index includes 0007; content matches `config_hash`.
- `docs/user/install.md:9` "Python 3.11+" — confirm against
  `pyproject.toml` `requires-python` (venv is 3.13); flag only.

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

- **NIC addressing redesign: `NetworkIface.addr` sum type + `nic_idx` SSH
  target.** (2026-05-21) Fixes the `dhcp4: true`-for-no-DHCP-NIC bug at its
  root — the old `ipv4: str | None` overloaded `None` to mean both "DHCP" and
  "no config." Replaced with `addr: DHCPAddr | StaticAddr | None`
  (`testrange/devices/network/base.py`):
  - `None` → unconfigured (`dhcp4: false, dhcp6: false, optional: true`); was
    the bug (rendered `dhcp4: true`). `DHCPAddr()` → `dhcp4: true`.
    `StaticAddr(addr, gw=None, dns=())` → static, frozen + hashable
    (`dns` is a tuple, for `config_hash`).
  - `StaticAddr` resolution: **explicit wins, else derive from the Switch, else
    raise** — only the prefix is ever underivable; absent gw/dns = isolated
    segment, valid. Lets a NIC dictate an unmanaged gateway. (See
    memory `feedback_switch_flags_describe_sidecar`: flags describe the
    sidecar, not the wire; out-of-band DHCP/gateway not policed.)
  - `SSHCommunicator(username, *, nic_idx=None)` selects which NIC to SSH to by
    position (the only thing that disambiguates multiple NICs on one network);
    `discover_ip` honors it, else uses the first *addressed* NIC. The
    communicator holds only the int — orchestrator still brokers.
  - Renderer (`cloudinit.py`), `validate.py` (only `StaticAddr` validated),
    `sidecar.py` (host-record for static, dhcp-host for `DHCPAddr` only),
    `run_phase.discover_ip`, `cli.py` display, `install.py` sidecar wiring
    (`None`→`DHCPAddr()`), and all examples + tests migrated off `ipv4=`.
  - 419 unit tests pass (new: `TestRunPhaseNetplanTriState`, `TestStaticAddr`,
    `TestNicIdxSelection`, NIC `addr` tests); ruff + mypy clean. PLAN §5/§10
    updated. **Live libvirt smoke test still pending** (moved to Short-term);
    docs/user rewrite for the new API also Short-term.

- **`.tours` code-review + fixlist sweep applied.** (2026-05-19)
  - *Blocking:* XML-escape snapshot/pool render + reject illegal chars in
    VM/Network/Switch/snapshot names at construction (`testrange/_names.py`);
    cap QGA guest-file read/write at 64 MiB; `teardown()` no longer bails on
    a `set_phase` failure; `QGACommunicator.close()` is one-shot and raises
    `CommunicatorClosedError` (state collapsed to one flag).
  - *Suggestions/nits:* `_qga_agent_not_ready` prefers
    `VIR_ERR_AGENT_UNRESPONSIVE`; bridge address-add is idempotent via a
    `get_addr` precheck (`_assign_bridge_addr`); `compose_mac` collision
    constraint documented (no `run_id` mix — ADR-0006); `_QGA_CHANNEL_NAME`
    constant; VNC local-only intent noted; signal-handler mid-test
    limitation documented; `AutoAddPolicy` ephemeral-only comment; cloud-init
    migrated to the modern `chpasswd.users[]` form + seed-ISO credential
    note; reserved `__` name prefix rejected at `LibvirtHypervisor`.
  - *Fixlist:* `Plan(name=...)` now required (examples/tests/docs updated);
    `--verbose` dropped; `PosixCred.extra_groups` → `groups`; `SSHKey` moved
    to `testrange.utils`; `no-DHCP + no-static` NIC now allowed
    (`validate.py`); Builder/recipe docstrings de-cross-cut; `networks/base`
    uplink prose → Driver→Semantics table; `state/cleanup` WHY docstrings;
    ISO `interchange_level`/`joliet`/`rock_ridge` magic values commented;
    ADR-0007 (deterministic `config_hash`) added and referenced.
  - *Verified addressed by prior work, dropped from tours:* per-RPC domain
    re-resolve (documented); uplink early-exit ordering (safe).
  - *Follow-up fixed same day:* DHCP discovery now reads the sidecar's
    dnsmasq lease file over QGA (`run_phase.discover_ip` +
    `parse_dnsmasq_leases`) instead of libvirt's `DHCPLeases()`, which is
    always empty for sidecar-served networks; the dead `get_lease_ip` was
    removed from the driver ABC + `LibvirtDriver`. This actually fixes
    DHCP-only VMs, which previously never resolved an IP.
  - ruff + mypy clean; 417 unit tests pass. Two remaining follow-ups moved
    to Short-term (DHCP-lease-vs-sidecar, sidecar image versioning).

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
