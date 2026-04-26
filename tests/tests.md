# TestRange test suite map

One-paragraph-per-file overview of what each test module covers and
why it exists.  Every file is a thin `pytest` collection; there are
no cross-file fixtures apart from what's in `conftest.py`.  Run with
`pytest` from the repo root.

## Harness

### `conftest.py`
Imports shared fixtures, ensures stub modules are on `sys.path`
before the first test import (so `libvirt`, `pycdlib`, `passlib`,
`paramiko`, `winrm` can be mocked cleanly on machines without the
real deps installed).  Provides the `tmp_cache_root` fixture — a
per-test temp dir shaped like a `CacheManager` root.

## Public API contracts

### `test_backend_contract.py`
Parametrised contract tests across every shipped backend
(`LibvirtOrchestrator`, `ProxmoxOrchestrator`).  Asserts each
subclass:
- is a real `AbstractOrchestrator` subclass,
- implements the context-manager protocol,
- accepts the four standard kwargs (`host`, `networks`, `vms`,
  `cache_root`),
- exposes `.vms: dict[str, AbstractVM]`.
Also guards the refactor invariant that `AbstractVM.build` and
`AbstractVirtualNetwork.start`/`stop` take `context=`, never the
old libvirt-specific `conn=`.  *Never* instantiates the Proxmox
classes — signature checks only.

### `test_builders.py`
Locks down the abstract `Builder` interface and verifies the three
concrete builders (`CloudInitBuilder`, `WindowsUnattendedBuilder`,
`NoOpBuilder`) satisfy it.  Covers `default_communicator()` mappings,
`needs_install_phase()` return values, and the `InstallDomain` /
`RunDomain` output shape each builder produces.  Also exercises
auto-selection: a VM built with a URL gets `CloudInitBuilder`, a
Windows ISO gets `WindowsUnattendedBuilder`, etc.  The
`needs_boot_keypress()` mapping (Windows → `True`, others → `False`)
is exercised in `test_vm_libvirt.py::TestBootKeypressSpam`.

### `test_proxmox_scaffold.py`
Asserts the Proxmox backend scaffolding is importable *without*
`proxmoxer` installed (lazy imports live inside `__enter__`), and
that every stubbed lifecycle method raises `NotImplementedError` with
a clear message that points future contributors at the TODO list in
the module docstring.  If a Proxmox implementation lands, the tests
that check for `"not yet implemented"` substrings will start failing
and need replacing with real behavioural assertions.

## Core orchestration

### `test_orchestrator.py`
The libvirt orchestrator's state machine.  URI resolution (localhost
shorthand vs full `qemu+ssh://` URI), install-network creation
(correct subnet selection, stale-network cleanup, VM DHCP
registration), NIC plumbing (`_build_nic_entries` for internet-on /
isolated / DHCP / static / unknown-network-ref cases), and the
install-free path (VMs with `NoOpBuilder` are routed around the
install network entirely).

### `test_teardown_resilience.py`
Belt-and-braces for the contract that `Orchestrator._teardown` never
raises.  Each test simulates a single-step failure (VM shutdown
raises, a network stop raises, the install network stop raises, the
run-dir cleanup raises, the connection close raises) and asserts the
other steps still execute.  Also asserts teardown ordering (VMs
before networks; networks before connection close) and full-failure
idempotency.  `test_keyboardinterrupt_during_enter_triggers_teardown`
guards the `BaseException` widening in `__enter__` — a Ctrl+C during
a long install wait must still run teardown before the interrupt
propagates, so no `tr-build-*` domain orphans under
`qemu:///system`.

### `test_vm_libvirt.py`
The libvirt `VM` class — constructor defaults, device accessors
(`_vcpu_count`, `_memory_kib`, `_primary_disk_size`), builder cache
key derivation, domain XML generation (well-formed XML, NVMe vs
virtio disk bus, multi-NIC output, guest agent channel always
emitted, no `<cmdline>` element), `shutdown()`, and the build-cache
lock contract (cache hit skips `_run_install_phase`; a second
identically-spec'd build hits the cache).  Also covers three
cross-cutting contracts: the `TESTRANGE_VNC=1` opt-in graphics
toggle (off by default, emits `<graphics type='vnc' listen='127.0.0.1'>`
when set); `TestInstallPhaseCleanup` — the install-phase `try/finally`
must always destroy + undefine the local `domain`, including on
`KeyboardInterrupt` or `VMBuildError` mid-wait; and
`TestBootKeypressSpam` — builders that return
`needs_boot_keypress()=True` get a short-lived thread that calls
`domain.sendKey(SPACE)`, builders that return `False` do not.
`shutdown()` also handles the stashed `_install_domain` as a safety
net, so an orchestrator teardown after a `finally`-block failure
still cleans up.

### `test_vm_prebuilt.py`
The BYOI path through the public API: a `VM` constructed with
`builder=NoOpBuilder()`.  Covers default communicator (`"guest-agent"`
for Linux, `"winrm"` for `windows=True`), `ready_image` staging
(content-hash copy into the cache, idempotent second call, in-place
use when the source is already under the cache root, missing-file
error, wrong-format error), static-IP resolution for SSH / WinRM
communicators, and the `build()` dispatch invariant: NoOp VMs go
through `builder.ready_image`, never `_run_install_phase`.

### `test_vm_windows.py`
The Windows install + run path.  Auto-selection (Windows ISO →
`WindowsUnattendedBuilder` → `communicator="winrm"` default),
domain-XML properties (UEFI loader + NVRAM template, boot order is
CD-ROM then HD, primary disk on SATA `sda`, CD-ROMs start at `sdb`,
NIC model is `e1000e`), and WinRM communicator construction (root
credential maps to built-in `Administrator`; static IP required;
falls back to first credential when no `root`).  Also includes
`test_bootable_cdrom_is_first`: regression for the UEFI boot-order
bug where the unattend seed ISO was taking `bootindex=1` and UEFI
fell through to the empty disk → EFI shell → indefinite hang.

## Provisioning internals

### `test_cloud_init.py`
Module-level helpers (`_hash_password`, `_native_packages`,
`_runcmd_entries`, `_user_entry`) plus the builder's YAML emitters
(`install_user_data`, `install_meta_data`, `run_user_data`,
`run_meta_data`, `run_network_config`).  Also covers
`write_seed_iso` via a `pycdlib` mock.

### `test_unattend.py`
`WindowsUnattendedBuilder.build_xml()` and
`write_autounattend_iso()`.  Covers computer name / timezone / admin
password / product key propagation, user-account creation
(Administrators group for sudo users), winget command emission, and
the mandatory final `shutdown /s /t 0` that completes the install.
Also asserts the builder is stateless across different VMs.
`test_product_key_nested_inside_userdata` is the regression for the
"can't read product key from the answer file" Setup error — the
Microsoft unattend schema requires `<ProductKey>` inside
`<UserData>` under `Microsoft-Windows-Setup`; anywhere else is
silently ignored.  `test_default_product_key_emitted` guards the
default (Windows 10/11 Pro generic install key) that makes
multi-edition consumer ISOs install unattended out of the box.

### `test_networks.py`
The libvirt virtual network: MAC generation (QEMU OUI prefix,
deterministic, unique per VM/network pair), subnet math (gateway,
netmask, prefix, DHCP range, static-IP allocation), backend-name
truncation to 15 chars, VM-entry bookkeeping, XML generation (NAT
forward, DHCP / DNS entries), and the lifecycle (`start` wraps
`libvirt.libvirtError`, `stop` idempotent before `start`).  Also
checks the ABC can't be instantiated directly.

## Building blocks

### `test_cache.py`
`vm_config_hash` determinism + ordering invariants, `_sha256_file`,
`CacheManager` directory layout and permissions, `get_image`
download + cache-hit + failure paths, `stage_local_iso` (copy when
outside cache root, no-op inside, missing-source raises), and
`get_virtio_win_iso` download-once-then-cache behaviour.

### `test_qemu_img.py`
The thin `subprocess` wrappers around `qemu-img` — `create_overlay`,
`create_blank`, `resize`, `convert_compressed`, `info`.  Asserts
correct argv construction and that non-zero exits surface as
`CacheError`.

### `test_run.py`
`RunDir` creation (world-readable mode so libvirt-qemu can read
overlay disks), overlay + blank-disk + seed-ISO + NVRAM path
generation, and cleanup idempotency.

### `test_devices.py`
`vCPU`, `Memory`, `HardDrive`, `vNIC` validation (size
parsing, `nvme=` toggles, default values), plus shared helpers
(`parse_size`, `normalise_qemu_size`).

### `test_packages.py`
Every shipped package class (`Apt`, `Dnf`, `Pip`, `Homebrew`,
`Winget`) — `package_manager`, `native_package_name`,
`install_commands`, `repr`.  Locks in the Homebrew templating
convention (`{brew_user}` placeholder) that the cloud-init builder
relies on.

### `test_credentials.py`
`Credential.is_root()` + defaults + frozen-dataclass behaviour.

### `test_images.py`
`resolve_image` (URL download path vs local-path passthrough, error
when neither) and `is_windows_image` heuristics (`.iso` +
Windows-ish filename).

### `test_exceptions.py`
Exception hierarchy smoke checks — every bespoke exception inherits
from `TestRangeError`.

## Communication backends

### `test_communication.py`
The libvirt QEMU guest-agent communicator.  JSON-RPC wire format,
`wait_ready` polling, `exec` parsing (exit code + captured output),
`get_file` chunk concatenation, `put_file`, `hostname`, and
timeout handling.

### `test_communication_ssh.py`
`SSHCommunicator` with a stubbed `paramiko`: connection retry on
banner errors, `exec` with and without `env`, SFTP-backed
`get_file` / `put_file`, and the error wrapping.

### `test_communication_winrm.py`
`WinRMCommunicator` with a stubbed `pywinrm`: transport selection
(HTTP/HTTPS, NTLM/basic), `exec` via `cmd.exe` vs PowerShell,
chunked base64 uploads, and non-zero-exit error propagation.

## High-level test execution

### `test_test.py`
`Test` + `run_tests`: passing/failing result shapes, traceback
capture, concurrency (sequential vs parallel) and result ordering
under each.

### `test_cli.py`
The `testrange` CLI.  `run` / `describe` / `repl` command parsing,
target-form resolution (`module:factory`), test selection by name,
the `--orchestrator` backend-override flag (default libvirt, unknown
backend rejected, proxmox backend requires `--proxmox-token`), and
the cache-list / cache-clear commands.

### `test_repl.py`
`_repl.start_repl` branching (IPython vs stdlib `code.InteractiveConsole`),
the `--keep` post-exit summary, and the namespace the REPL is
launched with (`orch`, `vms`, one binding per VM name).

### `test_regressions.py`
One-liner regressions tied to specific past bugs / design
decisions.  MAC OUI prefix, Homebrew requires a non-root user,
`qemu-guest-agent` always present in install packages, network-name
length cap, builder statelessness, and `bind_run` clears prior
entries.  Any new bug we've fixed and don't want to come back should
land here.
