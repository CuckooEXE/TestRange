# Changelog

All notable changes to this project are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
This project predates 1.0; expect breaking changes between minor versions.

## [Unreleased]

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
