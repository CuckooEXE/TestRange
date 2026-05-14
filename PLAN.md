# TestRange — Design Plan

Living design document for `testrange`, a Python framework for declarative VM
test-ranges. Iterate here until we're ready to start coding.

## Background

Users write declarative Python scripts that describe VMs on a hypervisor in
specific configurations (networking, disks, etc.). User-supplied test
functions then run against those VMs with handles into the orchestrator.
Use cases: CI/CD against specific OS versions and varied network
topologies; authorized pentest test-ranges.

Goals: maximum functionality from sane defaults, simple discoverable API,
no leaky abstractions, stovepiped components (the orchestrator is the only
component allowed to know multiple stovepipes and broker between them).

The predecessor at `.bak/` is a failed refactor; it serves as a
lessons-learned repository (anti-patterns to avoid, a small set of
high-level concepts that proved valuable). No code or structural design
ports from `.bak`.

## Design Decisions

### 1. VM type: split into `VMSpec` + `VMRecipe`

`VMSpec` is hardware (name, devices). `VMRecipe` is provisioning (spec,
builder, communicator, packages). `VMHandle` is the runtime view exposed
to test code. No god-class.

```python
VMRecipe(
    spec=VMSpec(
        name="webserver",
        devices=[
            CPU(2),
            Memory(4096),                       # MB
            OSDrive("pool1", 64),               # GB; exactly one per spec
            HardDrive("pool2", 128),            # data disk; many allowed
            LibvirtNetworkIface("netB", driver="e1000"),
        ],
    ),
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[
            PosixCred("root", password="..."),
            PosixCred("myuser", pubkey=key.public, sudo=True),
        ],
        packages=[Apt("nginx")],
        post_install_commands=("echo hi > /tmp/hi",),
    ),
    communicator=SSHCommunicator("myuser"),
)
```

### 2. `Plan(*hypervisors)`

Variadic from day 1; v0 enforces exactly one at runtime. Multi-hypervisor
is a long-term TODO that does not break the call shape.

### 3. Top-level Hypervisor is its own class, NOT a VM

For v0, `LibvirtHypervisor(connection=..., networks=..., pools=..., vms=...)`
is the top-level Plan entry. It is the *host*, not a VM. The driver is
inferred from the Hypervisor type (`LibvirtHypervisor` → `LibvirtDriver`).
Nested hypervisors are explicitly **out of scope for v0**. When nesting
lands, it lands as a separate class shape — designed fresh.

### 4. State schema future-proofs for resume + nested; feature deferred

State schema is versioned and includes intent_at/outcome_at timestamps and
a `metadata` dict per resource so resume can be added without schema
migration. No `--resume` flag in v0 — fields exist, runtime ignores them.
Schema version 1 from day one.

### 5. Communicator: direct construction + per-type `bind(...)`

Plan declares a Communicator instance; orchestrator binds it at run-phase
bring-up. **Communicator never holds a driver or backend ref**. There is
no uniform "handle" — different communicators need different inputs.

```python
class SSHCommunicator(Communicator):
    def __init__(self, username: str): ...
    def bind(self, *, host: str, credential: PosixCred) -> None: ...

class QGACommunicator(Communicator):
    def __init__(self): ...
    def bind(self, *, exec_fn: Callable[..., ExecResult]) -> None: ...
```

The orchestrator dispatches by communicator type (it's the broker per the
stovepipe rule):

- For `SSHCommunicator`: orchestrator passes the resolved IP + the
  credential looked up from `builder.credentials` by `username=`.
- For `QGACommunicator`: orchestrator passes a driver-supplied `exec_fn`
  callable that wraps the libvirt domain ref in a closure. QGA itself
  never sees a libvirt type.

Single-use guard on each concrete so a Communicator reused across two VMs
fails loud. No `clone()`, no install-phase binding — install is
builder-driven.

### 6. Credentials live on `Builder`; orchestrator brokers to Communicator

Builder doesn't know about Communicator, and vice versa. Credentials are
declared on the Builder (it bakes them into the disk). At Communicator
bind time, the orchestrator pulls `builder.credentials`, resolves the
username, and hands the matched credential into the Communicator's bind.

Builder concretes (e.g., `CloudInitBuilder`) MAY isinstance-check
Credential subtypes inside their own implementation — that's
intra-stovepipe dispatch, not cross-stovepipe reach.

### 7. Auth precedence: pkey if present, else password

`PosixCred("user", password="p", pubkey=k)` carrying both is legal data,
but `SSHCommunicator` presents **exactly one** auth method to paramiko
per attempt: `pkey=` when present, else `password=`. Deterministic.

### 8. `OSDrive` is a distinct class

```python
devices=[CPU(2), Memory(4096), OSDrive("pool1", 64),
         HardDrive("pool2", 128), LibvirtNetworkIface("netB")]
```

Exactly one `OSDrive` per `VMSpec` (runtime check). `HardDrive` is a data
disk.

### 9. Singleton-device runtime check

`VMSpec.__post_init__` enforces: exactly one CPU, exactly one Memory,
exactly one OSDrive, ≥ zero HardDrives, ≥ zero NetworkIfaces.

### 10. Switch is an L2 broadcast domain (vSwitch model)

Switch maps to ESXi's vSwitch (and analogous primitives on other drivers).
Networks on the same Switch share one L2 broadcast domain. Cross-Switch
traffic is dropped.

**Libvirt implementation**: each Switch is one OpenvSwitch (or Linux)
bridge; each Network within is a port-group with a VLAN tag (OVS-backed)
or a sub-network on the shared bridge. The OVS-backed model requires
`openvswitch-switch` on the host; surfaced via preflight.

Different subnets on the same Switch do not naturally route between each
other — they're on the same wire but distinct IP spaces. A gateway VM or
user-side routing is required. Long-term TODO: a `Switch(gateway=True)`
kwarg that brings up an implicit router VM.

### 11. ISOs / base disks referenced ONLY via `CacheEntry`

URLs and filepaths are dropped from Plan-time entirely. The only way to
use a base disk is to first `testrange cache add` it.

**`testrange cache add <path-or-url> [--name <pretty>]`**: ingests the
source, computes content sha, stores at
`$XDG_CACHE_HOME/testrange/isos/<sha>.bin`, writes a sidecar
`<sha>.json` with metadata. Prints the sha to stdout.

**`CacheEntry("identifier")`**: single positional string. Auto-detect:
matches `^[0-9a-f]{16,64}$` → content hash; otherwise pretty-name.
Resolution scans sidecars to map name → sha.

**Multiple aliases per entry**: rename adds to `names[]`.
`cache forget-name <name>` removes one alias. Names are globally unique
within the cache.

**HTTP cache**: `--cache https://…` injects an HTTP tier. Reads:
local → HTTP → miss. Writes (during `cache add`): local always; HTTP
best-effort. Plans never reference HTTP URLs directly.

**Sidecar schema (`<sha>.json`)**:
```json
{
  "sha256": "abc123def...",
  "size": 419430400,
  "names": ["debian-13", "debian-trixie"],
  "origin": "https://cloud.debian.org/.../debian-13-...qcow2",
  "added_at": "2026-05-10T18:30:00Z",
  "description": null
}
```

Format detection (qcow2 vs raw vs vmdk) is a driver concern, not a cache
concern. The libvirt driver inspects the resolved path when it needs to;
the cache layer treats every entry as opaque bytes.

### 12. CacheEntry miss fails at preflight, not bring-up

`driver.preflight(plan)` is read-only and called after `connect()` but
before any `state.json` write. The plan-level preflight helper collects
all `CacheEntry` references and verifies each resolves; misses go in the
report as errors with a `fix_hint`:
`testrange cache add <…> --name <…>`. `describe` is best-effort —
missing entries print "⚠ not in cache" but do not error.

### 13. `execute(argv)` returns `ExecResult`

```python
result = vm.communicator.execute(["systemctl", "is-active", "nginx"],
                                  timeout=10.0)
# ExecResult(exit_code: int, stdout: bytes, stderr: bytes, duration: float)
```

Argv-list only; no shell. Read-modify symmetric:
- `read_file(path) -> bytes`
- `write_file(path, data: bytes) -> None`

Streaming variant (`execute_streaming`) deferred.

### 14. Test execution: sequential, continue-on-failure default

All tests run in declaration order. Failure → log and continue. CLI flag
`--fail-fast` opts in to stop-on-first-failure. Tests share the
brought-up range (mutations bleed between tests). Long-term TODO:
per-test snapshot/revert.

### 15. No `subprocess` in v0

`subprocess` is forbidden project-wide in v0. Every v0 operation has a
Python library option: `libvirt-python` for the hypervisor, `paramiko`
for SSH, `pycdlib` for cloud-init seed ISO authoring, `requests` for
HTTP. Day-1: ruff rule + CI gate that rejects `import subprocess`
anywhere in the package.

If a future feature requires a subprocess (`qemu-img` for cross-format
disk conversion, etc.), it gets its own ADR and a single sanctioned
module at that time.

### 16. Sync, single-threaded v0

Every dependency (libvirt-python, paramiko, requests, pycdlib) is
blocking. v0 runs single-threaded — install brings up one VM at a
time, tests run sequentially. No `asyncio`, no `ThreadPoolExecutor`.
Public API is sync.

State-file safety:
- Each `state.json` write is `.tmp` + `os.replace` (atomic on every
  modern filesystem). Protects against torn writes within a process.
- A sibling `state.pid` file records the owning process PID.
  `testrange cleanup <run-id>` reads it and refuses to act on a run
  whose PID is still alive (clear error: "PID <X> still alive; kill
  it first or wait for it"). Simpler than a FileLock and produces a
  meaningful error message.

Parallel install pass and cross-process locking are long-term TODOs.

### 17. Cleanup-on-failure CLI flag: `--leak-on-failure`

Mutually exclusive with the future `--resume`.

### 18. Storage locations follow XDG semantics

- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.json` — run state.
- `$XDG_CACHE_HOME/testrange/isos/<sha>.bin` + `<sha>.json` —
  content-addressed cache.

State and cache are independently disposable.

### 19. Builder declares run-phase readiness; orchestrator brokers

`SSH up != system ready`. A run-phase VM's SSH service is ordered
`After=cloud-init.target`, but `cloud-init.target` is reached after
only the second of four cloud-init stages (local → network → config →
final). `cloud-config.service` and `cloud-final.service` keep running
after SSH accepts connections; modules in those stages may still
rewrite hostname, `/etc/hosts`, etc. Tests that read system state in
that window race the finalizer.

Without a builder-defined ready signal, every Plan author has to
write the same `cloud-init status --wait` boilerplate as their first
test (see the pre-rework `hello_world.py` and `private_public.py`).
That's a leaky abstraction — users shouldn't need to know cloud-init
has multiple stages, and a plan that forgets the wait silently
race-conditions in CI.

**Decision: each Builder runs its own "ready for tests" check; the
orchestrator brokers an `execute` callable.** Between Communicator bind
and yielding the `OrchestratorHandle`, the orchestrator hands
`builder.wait_ready` the VM's `execute` callable — a `GuestExec` from
`testrange/guest_io.py`, which is the *shape* of `Communicator.execute`,
not a Communicator type. The builder runs whatever readiness command it
needs, inspects the `ExecResult`, and raises `BuildNotReadyError`
itself. Builder never imports Communicator — it only sees a callable.

#### Shape

```python
class Builder(ABC):
    def wait_ready(
        self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec
    ) -> None:
        """Block until the brought-up VM is ready for test code. Default:
        no-op — for builders that produce a fully-baked disk with no
        post-boot finalization. Concretes run the readiness command via
        ``execute`` and raise ``BuildNotReadyError`` if the VM never
        becomes ready."""
```

`CloudInitBuilder` overrides:

```python
def wait_ready(self, spec, recipe, execute):
    r = execute(("cloud-init", "status", "--wait"), timeout=300.0)
    if r.exit_code != 0:
        raise BuildNotReadyError(...)
```

The orchestrator, in `__enter__` after `_bind_communicators`:

```python
for vm in self.plan.hypervisor.vms:
    try:
        vm.builder.wait_ready(vm.spec, vm, vm.communicator.execute)
    except BuildNotReadyError as e:
        raise BuildNotReadyError(f"vm {vm.name!r}: {e}") from e
```

#### Why a callable, not argv

An earlier cut returned `tuple[str, ...] | None` (argv) and let the
orchestrator run it — purely to keep the Communicator type off
Builder's signature. The `GuestExec` callable type (`guest_io.py`)
makes that unnecessary: it's the shape of `Communicator.execute`, not
a Communicator. The builder gets the ability to run a command — and to
interpret the `ExecResult`, retry, probe multiple things — without
ever seeing a Communicator. It's the same shared callable type the QGA
communicator's `bind` consumes (see §20).

#### The timeout

The builder owns it. `CloudInitBuilder.wait_ready` passes
`timeout=300.0` inline — a cold boot's `cloud-final` stage genuinely
takes minutes, and that named problem lives at the call site, not in a
framework-wide knob. No orchestrator `ready_timeout_s`.

#### Error type

`BuildNotReadyError(BuilderError)`. Distinct from `BuilderError` so
callers can catch "VM came up but never reached ready" narrowly. CLI
exit code stays at 1 (general orchestrator failure) — no new
dedicated exit code.

#### Example impact

`examples/hello_world.py` and `examples/private_public.py` carry no
`cloud_init_finished` test. The orchestrator's bring-up sequence:
preflight → install → run → **bind** → **wait-ready** → hand off
`OrchestratorHandle` to tests.

#### Files touched

- `testrange/guest_io.py` — **new**; `GuestExec` (+ `GuestReadFile` /
  `GuestWriteFile`) live here, plus a re-export of `ExecResult`.
- `testrange/builders/base.py` — `wait_ready(spec, recipe, execute)`
  on the ABC, non-abstract no-op default.
- `testrange/builders/cloudinit.py` — override runs `execute` and
  raises `BuildNotReadyError`.
- `testrange/orchestrator/runtime.py` — call site after
  `_bind_communicators`; no timeout knob.
- `testrange/exceptions.py` — `BuildNotReadyError`.
- `examples/hello_world.py`, `examples/private_public.py` — no
  `cloud_init_finished` test.
- `tests/unit/test_guest_io.py`, `tests/unit/test_cloudinit.py`,
  `tests/unit/test_orchestrator.py` — coverage.
- `docs/` — readiness is the orchestrator's job; no `cloud-init status
  --wait` test needed.

### 20. QGA communicator: driver owns the wire protocol, communicator is a shim

`SSHCommunicator` is not always usable: an air-gapped VM with no
management network has no IP to reach, and even on a networked VM SSH
is not up until late in boot. Every hypervisor with a native in-guest
agent (libvirt/QGA, ESXi/VMware Tools, Proxmox/QGA) offers an in-band
exec channel that sidesteps both problems.

**Decision: the driver owns the agent wire protocol; the communicator
is a thin shim over loose callables.** The driver exposes three
optional-capability accessors —
`native_guest_execute`/`native_guest_read_file`/`native_guest_write_file`
— each returning a VM-bound callable typed as the matching `guest_io`
Protocol (`GuestExec`/`GuestReadFile`/`GuestWriteFile`). `QGACommunicator`
takes those three callables in `bind` and delegates; it imports nothing
driver-side. The orchestrator is the broker — it pulls the callables
off the driver and hands them over.

Loose callables, not a bundle object: a future native agent might not
expose every operation, and three independent callables leave room for
that without a rigid all-or-nothing Protocol. Nothing optional is built
now — all three are required at `bind`.

#### Libvirt concretes

- `_import_libvirt_qemu()` — lazy import mirroring `_import_libvirt`
  (`libvirt_qemu` ships inside `libvirt-python`; same `.[libvirt]`
  extra, no new dependency).
- `_LibvirtGuestAgent` — VM-bound, re-resolves the domain per call,
  speaks the QGA JSON protocol over `libvirt_qemu.qemuAgentCommand`
  (`guest-exec` + `guest-exec-status` poll; `guest-file-open/read/
  write/close`; `cwd` shimmed via `sh -c`). Tolerates a not-yet-up
  agent with a bounded retry, the same shape as
  `SSHCommunicator._ensure_connected`. Wraps libvirt errors and QGA
  `{"error": ...}` responses into `GuestAgentError`.
- Every libvirt domain renders an `org.qemu.guest_agent.0` virtio
  `<channel>` unconditionally — inert without the guest package,
  and it avoids a cross-stovepipe `isinstance` in the driver.

#### `qemu-guest-agent` is user-declared

The guest needs `qemu-guest-agent` installed and running.
`CloudInitBuilder` is *not* changed to auto-inject it — that would be
the builder peeking at the communicator type. The plan author declares
`Apt("qemu-guest-agent")` + a `systemctl enable --now` line. A plan
that forgets it fails at the first `execute` with a clear
`GuestAgentError`.

#### Error type

`GuestAgentError(DriverError)`. A brought-up VM whose agent never
answers surfaces here.

#### Files touched

- `testrange/guest_io.py` — the shared Protocols (also used by §19).
- `testrange/exceptions.py` — `GuestAgentError`.
- `testrange/drivers/base.py` — the three `native_guest_*` accessors.
- `testrange/drivers/libvirt.py` — `_import_libvirt_qemu`,
  `_LibvirtGuestAgent`, the accessors, the QGA `<channel>`.
- `testrange/communicators/qga.py` — `QGACommunicator`.
- `testrange/communicators/__init__.py` — re-export.
- `testrange/orchestrator/runtime.py` — QGA branch in
  `_bind_communicators`.
- `examples/qga.py`, `tests/integration/test_libvirt_qga.py`,
  `tests/unit/test_qga_communicator.py`,
  `tests/unit/test_libvirt_driver_unit.py`,
  `tests/unit/test_drivers_base.py` — example + coverage.

## v0 example (target shape)

```python
"""hello_world: one libvirt VM, cloud-init bootstraps SSH + nginx, smoke-test it.

Prerequisites:
  testrange cache add https://cloud.debian.org/.../debian-13-generic-amd64.qcow2 \
      --name debian-13
"""
from __future__ import annotations
import sys

from testrange import Plan, OrchestratorHandle, run_tests
from testrange.cache import CacheEntry
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Switch, Network
from testrange.devices import (
    CPU, Memory, OSDrive, HardDrive, StoragePool,
    LibvirtNetworkIface,
)
from testrange.vms import VMSpec, VMRecipe
from testrange.builders import CloudInitBuilder
from testrange.credentials import PosixCred, gen_ssh_key
from testrange.communicators import SSHCommunicator
from testrange.packages import Apt


_KEY = gen_ssh_key(comment="testrange-hello")

PLAN = Plan(
    LibvirtHypervisor(
        connection="qemu:///session",
        networks=[
            Switch("switch1", mgmt=True, internet=True,
                Network("netA", "172.31.0.0/24", dhcp=True, dns=True),
                Network("netB", "10.10.10.0/24"),
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="web",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        LibvirtNetworkIface("netA", driver="virtio"),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred("root", password="root"),
                        PosixCred(
                            "myuser",
                            password="mypass",
                            pubkey=_KEY.public,
                            sudo=True,
                        ),
                    ],
                    packages=[Apt("nginx")],
                    post_install_commands=("echo hi > /tmp/hi",),
                ),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)


def cloud_init_finished(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(
        ["cloud-init", "status", "--wait"], timeout=300.0
    )
    assert r.exit_code == 0, r


def nginx_is_installed(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["dpkg", "-l", "nginx"])
    assert r.exit_code == 0, "nginx missing"


def hostname_matches(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["hostname"])
    assert r.stdout.strip() == b"web", r


TESTS = [cloud_init_finished, nginx_is_installed, hostname_matches]

if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
```

## v0 phases

Each phase has explicit state transitions so that an interrupted run can
be cleaned up via state-file-driven `testrange cleanup`.

1. **Pre-Flight** — read-only. Driver-side host checks (libvirt-python
   reachable, pool writable, disk capacity). Plan-side checks (subnet
   overlap, static-IP-out-of-CIDR, name uniqueness, singleton-device
   counts, CacheEntry resolvable). Returns
   `PreflightReport(errors, warnings)`. Errors abort; warnings advisory.

2. **Install** — per-VM, builder-driven, cache-aware:
   - Compute `builder.config_hash(spec, recipe)` — deterministic 16-char
     hex. Pure (no I/O, no `run_id`).
   - Cache hit on `config_hash` → skip to phase 3.
   - Cache miss: builder produces a **self-terminating install payload**
     (cloud-init seed whose final `runcmd` is `poweroff`). Orchestrator
     creates a transient install VM on a transient internet-connected
     install network, boots it, and **polls driver-level power-state** —
     no communicator — until the VM signals install-done by shutting
     down. On done: snapshot the post-install disk into the cache; tear
     down the install VM and network.
   - **All install resources recorded in state.json BEFORE create-call.**

   Communicators are not used during install. The builder owns the
   install lifecycle end-to-end via its own seed configuration plus
   driver-level probes (power state).

3. **Run**:
   - User-declared networks and pools come up.
   - For each VM: clone the cached post-install disk into the run pool;
     define + start the run VM (no seed ISO attached); communicator
     binds.

4. **Test** — sequential, continue-on-failure default. Each test gets an
   `OrchestratorHandle` exposing `.vms[name]`, `.networks[name]`,
   `.pools[name]`, `.run_id`.

5. **Cleanup** — unless `--leak-on-failure` and any test failed:
   - Power off all VMs (graceful, then destroy on timeout).
   - Tear down VMs, networks, pools, in LIFO order from state.json.
   - Remove state.json on success.

## CLI surface (v0)

```
testrange --verbose --log-level {debug,info,warn,error}
testrange --cache https://… <subcommand>          # HTTP cache injection

testrange cache add <path-or-url> [--name <pretty>] [--description <text>]
testrange cache list
testrange cache del <hash-or-name>
testrange cache rename <hash-or-name> <new-name>
testrange cache forget-name <name>

testrange describe <plan.py>                       # passive; cache warnings only
testrange run <plan.py>                            # bring up + tests + cleanup
testrange run --leak-on-failure <plan.py>
testrange run --fail-fast <plan.py>

testrange cleanup <run_id>
testrange cleanup --all
testrange cleanup --all --dry-run
```

Exit codes: 0 = success; 1 = test failure; 2 = preflight failure;
3 = cleanup failure; ≥ 64 = unexpected internal error.

## File layout (v0)

```
docs/
    user/                       # user-facing guides
    dev/                        # contributor docs
    adr/                        # architecture decision records
    Architecture-and-Design.md
examples/
    hello_world.py
testrange/
    builders/
        base.py                 # Builder ABC
        cloudinit.py            # CloudInitBuilder
    cache/
        __init__.py             # CacheEntry exposed here
        local.py                # LocalCache (file-based, sidecar JSON)
        http.py                 # HttpCache (best-effort)
        manager.py              # CacheManager (local + http tiers)
    communicators/
        base.py                 # Communicator ABC: execute / read_file / write_file
        ssh.py                  # SSHCommunicator (paramiko)
    credentials/
        base.py                 # Credential ABC (pure data)
        posix.py                # PosixCred
    devices/
        cpu/{base.py, generic.py, libvirt.py}
        memory/{base.py, generic.py, libvirt.py}
        disk/{base.py, generic.py, libvirt.py}    # OSDrive + HardDrive
        network/{base.py, generic.py, libvirt.py} # iface, network, switch
        pool/{base.py, generic.py, libvirt.py}    # StoragePool
    drivers/
        base.py                 # HypervisorDriver ABC
        libvirt.py              # LibvirtDriver + LibvirtHypervisor
    networks/
        base.py                 # Network, Switch ABC
        libvirt.py              # libvirt concretes
    orchestrator/
        runtime.py              # Orchestrator, OrchestratorHandle, VMHandle
        phases.py               # preflight / install / run / test / cleanup
    packages/
        base.py
        apt.py
        pip.py
    state/
        store.py                # state.json + state.pid; atomic-rename writes
        schema.py               # version 1 dataclasses
        cleanup.py              # state-file-driven teardown (PID-checked)
    vms/
        spec.py                 # VMSpec
        recipe.py               # VMRecipe
        handle.py               # VMHandle (runtime view)
    _log.py                     # stdlib logging w/ run_id LoggerAdapter
    cli.py                      # argparse → subcommands
    exceptions.py
tests/
    unit/
    integration/                # gated by pytest -m libvirt
```

Stubs for proxmox / esxi / winrm are NOT exported until they work (no
Hyrum's-law re-exports of `NotImplementedError` shims).

## v0 Engineering Phases

Goal: walk from empty repo to `examples/hello_world.py` passing
end-to-end. Each phase ends with a green test suite (unit + the
integration tests that phase enables); no half-finished state crosses
a phase boundary. TDD per phase — tests land before/alongside the code
they cover. Dependency chain is linear: 0 → 1 → 2 → 3 → 4 → 5 → 6.

Risk-front-loading rationale: phases 2–4 are where libvirt-specific
surprises live (pool semantics, disk snapshot APIs, MAC handling,
cloud-init quirks). Knocking those out before SSH/test-runner means if
libvirt blows up, we discover it early without having built a test
runner against vapor.

### Phase 0 — Skeleton & Plan-time data types

- `pyproject.toml` with deps (`libvirt-python`, `paramiko`, `pycdlib`,
  `requests`); `ruff` + `mypy --strict` config; custom ruff rule that
  forbids `import subprocess`; `pytest` with a `libvirt` mark.
- `_log.py` (stdlib `logging` + run-id `LoggerAdapter`),
  `exceptions.py`, `cli.py` argparse skeleton (subcommands print
  "not implemented").
- All the pure-data classes `hello_world.py` imports: `Plan`,
  `LibvirtHypervisor`, `Switch`, `Network`, `StoragePool`, `CPU`,
  `Memory`, `OSDrive`, `HardDrive`, `LibvirtNetworkIface`, `VMSpec`,
  `VMRecipe`, `CloudInitBuilder` (data only), `CacheEntry`, `PosixCred`,
  `SSHCommunicator` (unbound), `Apt`, `gen_ssh_key`.
- Singleton-device runtime checks (`VMSpec.__post_init__`).
- Pretty-print `testrange describe examples/hello_world.py` walks
  the tree.

**Done**: `python examples/hello_world.py` imports cleanly; `testrange
describe` prints topology (CacheEntry shows ⚠ since cache doesn't
exist yet); 100% unit coverage of the data classes.

### Phase 1 — Cache layer + cache CLI

- `LocalCache` with `<sha>.bin` + `<sha>.json` sidecar layout, atomic
  writes via `.partial` + `os.replace`.
- `CacheManager` (local tier only; HTTP tier deferred).
- CLI: `cache add <path-or-url> [--name <pretty>]`, `cache list`,
  `cache del`, `cache rename`, `cache forget-name`.
- `CacheEntry` resolves via the manager.
- `describe` shows CacheEntry resolution status.

**Done**: `testrange cache add https://cloud.debian.org/...qcow2 --name
debian-13` followed by `testrange describe hello_world.py` shows the
entry resolved with size + origin. Unit tests cover add/list/del/rename/
forget-name + sha computation + sidecar round-trip.

### Phase 2 — Libvirt driver foundation + state machinery

- `HypervisorDriver` ABC: `connect`, `disconnect`,
  `preflight(plan) → PreflightReport`, network/pool CRUD,
  `compose_resource_name`, `compose_mac(plan, vm, nic_idx)`.
- `LibvirtDriver`: `connect` via libvirt-python, `preflight` (read-only
  checks: subnet overlap, pool writable, name uniqueness, CacheEntry
  resolvable, etc.), network + pool create/destroy.
- State layer: `state.json` + `state.pid`, atomic-rename writes,
  record-before-create discipline, PID-checked cleanup.
- CLI: `cleanup <run-id>`, `cleanup --all`, `cleanup --all --dry-run`.

**Done**: an integration test (`-m libvirt`) creates a libvirt network
and pool through the driver, asserts they exist via libvirt's API,
then `testrange cleanup <run-id>` removes them and the state dir.
Preflight returns clean for `hello_world.py`.

### Phase 3 — VM CRUD + CloudInitBuilder seed

- `LibvirtDriver`: VM define, attach disk, attach NIC, start, shutdown
  (graceful → destroy on timeout), destroy. Stable MAC via
  `compose_mac`.
- `Builder` ABC.
- `CloudInitBuilder`: render user-data + meta-data + network-config;
  build seed ISO via `pycdlib`; `config_hash` (pure, deterministic).
- Packages (`Apt`, `Pip`) and `post_install_commands` plumbed into the
  cloud-init render.

**Done**: an integration test boots a VM by hand (driver + builder, no
orchestrator yet) with a known base disk and a seed ISO; asserts via
libvirt's domain APIs that the VM reaches `running` and then `shutoff`
after cloud-init's `poweroff`. `config_hash` is stable across two
renders of the same recipe.

### Phase 4 — Orchestrator: install + run phases

- `Orchestrator` class: `__enter__` / `__exit__`, phase sequencing
  (preflight → install → run → cleanup).
- Install phase: build seed → define install VM on a transient
  internet-NAT network → start → poll driver power-state until
  `shutoff` → snapshot post-install disk into cache (keyed by
  `config_hash`) → tear down install VM + transient network. All
  resources recorded in `state.json` before each create-call.
- Run phase: cache hit → clone overlay from cached base → define run
  VM with no seed → start.
- `LibvirtDriver`: disk snapshot (libvirt volume APIs), disk
  clone-overlay.
- Cleanup phase: LIFO teardown from `state.json`.

**Done**: `testrange run examples/hello_world.py` brings the range up,
lets cloud-init complete, tears it down (no tests executed yet — just
the bring-up/teardown loop). Second run hits the cache and skips the
install VM entirely.

### Phase 5 — SSH communicator + test runner

- `Communicator` ABC (`execute`, `read_file`, `write_file`, `close`;
  no `bind` in ABC — per-type).
- `SSHCommunicator.bind(host, credential)`: paramiko connect with
  retry (sshd takes time after boot); `execute(argv, timeout)` returns
  `ExecResult(exit_code, stdout, stderr, duration)`; `read_file` /
  `write_file` via SFTP; single-use guard.
- `VMHandle` runtime view (`.communicator`, convenience pass-throughs).
- `OrchestratorHandle` (`.vms`, `.networks`, `.pools`, `.run_id`).
- Test runner: import `plan.py`, discover `PLAN` + `TESTS`, run
  preflight/install/run, execute tests sequentially with
  continue-on-failure, return `list[TestResult]`. `run_tests` entry
  point.

**Done**: `testrange run examples/hello_world.py` brings up, runs all
three tests, all three pass, tears down. `python examples/hello_world.py`
exits 0.

### Phase 6 — Polish, signal handling, docs

- `--leak-on-failure`, `--fail-fast`, `--verbose`, `--log-level`.
- SIGINT / SIGTERM handler that transitions through cleanup before
  exit (vs `atexit`, which doesn't run on signals reliably).
- `cleanup --dry-run` listing.
- README with quickstart; `docs/user/install.md`,
  `docs/user/writing-a-plan.md`; `docs/Architecture-and-Design.md`.
- Minimal ADR set: subprocess ban, no asyncio, state-schema v1,
  CacheEntry-only, OSDrive distinct, driver-level stable MAC.

**Done**: README quickstart copy-pasteable on a clean machine works
first try (libvirt + KVM prerequisites assumed). Ctrl-C mid-run leaves
the host clean.
