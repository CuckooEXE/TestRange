# Changelog

All notable changes to this project are documented here.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
This project predates 1.0; expect breaking changes between minor versions.

## [Unreleased]

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
