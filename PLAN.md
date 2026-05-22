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
            NetworkIface("netB"),
        ],
    ),
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[
            PosixCred("root", password="..."),
            PosixCred("myuser", pubkey=key.auth_line, sudo=True),
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

A backend-specific Hypervisor dataclass (e.g. `MockHypervisor(...)`, the
reference backend; `ProxmoxHypervisor`, in progress) is the top-level Plan
entry, carrying `networks=`, `pools=`, `vms=` plus per-backend connection
config. It is the *host*, not a VM. The driver is inferred from the Hypervisor
type via the driver registry (`testrange/drivers/_registry.py`):
`MockHypervisor` → `MockDriver`. Nested hypervisors are explicitly **out of
scope for v0**. When nesting lands, it lands as a separate class shape —
designed fresh.

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
    def __init__(self, username: str, *, nic_idx: int | None = None): ...
    def bind(self, *, host: str, credential: PosixCred) -> None: ...

class NativeCommunicator(Communicator):
    def __init__(self): ...
    def bind(self, *, execute, read_file, write_file) -> None: ...  # three callables
```

The orchestrator dispatches by communicator type (it's the broker per the
stovepipe rule):

- For `SSHCommunicator`: orchestrator resolves the IP (`run_phase.discover_ip`)
  and passes it plus the credential looked up from `builder.credentials` by
  `username=`. The address comes from the NIC selected by `nic_idx` (its
  position in the device list — the only thing that disambiguates multiple
  NICs on one network), or, when `nic_idx is None`, the first NIC that carries
  an address. The communicator holds only the `nic_idx` int — never a NIC.
- For `NativeCommunicator`: orchestrator passes the driver-supplied
  `execute`/`read_file`/`write_file` callables that wrap the driver's VM-bound
  ref in closures. The communicator never sees a backend type (ADR-0008 §7).

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
         HardDrive("pool2", 128), NetworkIface("netB")]
```

Exactly one `OSDrive` per `VMSpec` (runtime check). `HardDrive` is a data
disk.

### 9. Singleton-device runtime check

`VMSpec.__post_init__` enforces: exactly one CPU, exactly one Memory,
exactly one OSDrive, ≥ zero HardDrives, ≥ zero NetworkIfaces.

### 10. Switch owns all networking-infrastructure knobs (ESXi-shaped)

A Switch is one L2 broadcast domain *and* the place every infrastructure
decision lives (`cidr`, `uplink`, `mgmt`, `dns`, `dhcp`, `nat`). A
Network is a logical label (port-group) within a Switch — VMs attach by
name. All Networks on a Switch share `switch.cidr`; multiple Networks
are organizational labels on one wire.

```python
Switch(
    name: str,
    *networks: Network,
    cidr: str = "192.168.10.0/24",   # strict network form; host-form raises
    uplink: str | None = None,        # physical NIC; testrange creates bridge(s)
    mgmt: bool = False,               # host adapter at .2 (NOT a router)
    dns: bool = False,                # sidecar serves DNS at .1
    dhcp: bool = False,               # sidecar serves DHCP at .1
    nat: bool = False,                # sidecar MASQUERADEs out uplink at .1
)
```

`nat=True` requires `uplink=` (the sidecar needs a physical NIC to
MASQUERADE traffic out of). Otherwise the flags are orthogonal; the
bare Switch is a pure L2 wire.

**Addressing pinning** (`testrange/networks/_addressing_consts.py`):

- `.1` — sidecar (iff `dhcp|dns|nat`); is the gateway when `nat=True`.
- `.2` — host mgmt adapter (iff `mgmt=True`).
- `.3`–`.9` — reserved.
- `.10`–`.99` — DHCP lease pool (iff `dhcp=True`).
- `.100`–`.254` — user statics.

**NIC addressing** (`testrange/devices/network/base.py`): the *Switch* owns
infrastructure; each *NIC* declares how it takes a run-phase address via
`NetworkIface.addr`, a three-case sum type:

- `None` (default) — the NIC is left **unconfigured** (no address, no DHCP).
  Renders `dhcp4: false`. (Not the same as DHCP — that distinction is the
  whole point; the old `ipv4`-or-`None` overload conflated them and shipped a
  bug where a no-DHCP NIC still rendered `dhcp4: true`.)
- `DHCPAddr()` — request a lease at boot. Renders `dhcp4: true` regardless of
  the Switch's `dhcp` flag (an out-of-band DHCP server is a legitimate
  topology; the flag only describes whether *our* sidecar serves leases).
- `StaticAddr("10.0.0.5/24", gw=..., dns=[...])` — a static address. Resolution
  per field: **explicit wins, else derive from the Switch, else raise.** Only
  the prefix is ever underivable (a static address needs a netmask); a missing
  gateway/DNS resolves to "isolated, no default route", which is valid. This is
  what lets a NIC point at an unmanaged gateway (a guest acting as a router).

The install phase always renders DHCP regardless of `addr` (install needs
internet); the run-phase netplan is staged separately via cloud-init
`write_files`.

**Sidecar VM** (`testrange/networks/sidecar.py`,
`testrange/builders/sidecar_iso.py`): a pre-built Alpine image with
`dnsmasq`, `nftables`, and `qemu-guest-agent` baked in
(`tools/build-sidecar-image/build.sh`). Per-Switch instance —
materialized only when `switch.needs_sidecar` (= `dhcp or dns or nat`).
Per-run config is delivered as a tiny ISO9660 (label `TR_SIDECAR_CFG`)
carrying `dnsmasq.conf`, `interfaces`, `nftables.nft`, `sysctl.conf`.

**NAT topology** (`nat=True, uplink="eth0"`): the driver realizes TWO L2
segments — an isolated switch segment (guests + sidecar's eth0 at `.1`, plus
the host's `.2` if `mgmt`) and a separate uplink segment enslaving the
physical NIC (sidecar's eth1, DHCP from upstream LAN). The sidecar's
`nftables` ruleset MASQUERADEs eth0→eth1. Without `nat`, an uplinked switch is
one segment (guests bridge directly to LAN with their own MACs).

**Driver owns L2 (ADR-0008 §1):** the driver realizes the full topology for a
Switch via `create_switch(switch, backend_name)` (and the uplink-facing
segment when `switch.uplink and switch.nat`); `create_network` attaches a
network to an already-created switch. The orchestrator never names a bridge —
all bridge/vSwitch/SDN mechanics live inside the driver. No backend-native
NAT/DHCP/DNS anywhere — the sidecar owns those uniformly across backends.

**Build phase** uses the same machinery: a transient build switch
(`cidr="10.97.99.0/24", uplink=hyp.build_uplink, dhcp=True, dns=True,
nat=True`) synthesized from `hyp.build_uplink`, brought up before build VMs
boot and torn down LIFO at build-phase end (ADR-0010 §9).

**Known limits** (TODO.md): host-local L2 (e.g. netlink-based bridge mgmt) is
local-only — `Switch.uplink`/`nat` over a remote backend connection are caught
by preflight (`remote_uplink_unsupported`). Multi-Network mgmt collapses
naturally now that Switches own one CIDR.

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
concern. The driver inspects the resolved path when it needs to;
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
Python library option: `proxmoxer` / `libvirt-python` for the hypervisor,
`paramiko` for SSH, `pycdlib` for cloud-init seed ISO authoring, `requests`
for HTTP. Enforced by a ruff rule + CI gate that rejects `import subprocess`
anywhere in the package (see `tests/unit/test_subprocess_ban.py`).

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

### 20. Native communicator: driver owns the wire protocol, communicator is a shim

`SSHCommunicator` is not always usable: an air-gapped VM with no
management network has no IP to reach, and even on a networked VM SSH
is not up until late in boot. Every hypervisor with a native in-guest
agent (libvirt/QGA, ESXi/VMware Tools, Proxmox/QGA, Hyper-V integration)
offers an in-band exec channel that sidesteps both problems.

**Decision: the driver owns the agent wire protocol; the communicator
is a thin shim over loose callables.** The driver exposes three
optional-capability accessors —
`native_guest_execute`/`native_guest_read_file`/`native_guest_write_file`
(each takes an optional `credential=`, for backends whose agent needs guest
creds — ADR-0008 §2) — returning a VM-bound callable typed as the matching
`guest_io` Protocol (`GuestExec`/`GuestReadFile`/`GuestWriteFile`).
`NativeCommunicator` (ADR-0008 §7: renamed from `QGACommunicator`, since the
shim is backend-agnostic) takes those three callables in `bind` and delegates;
it imports nothing driver-side. The orchestrator is the broker — it pulls the
callables off the driver and hands them over. The driver declares which ops it
supports via `native_guest_capabilities()`, and preflight fails loud on a gap
(ADR-0008 §3).

Loose callables, not a bundle object: a backend might not expose every
operation, and three independent callables leave room for that without a rigid
all-or-nothing Protocol.

#### Per-backend wire protocol (driver-internal)

The wire protocol lives entirely inside each driver. The reference is
`MockDriver`'s in-memory native transport. The libvirt concrete (QGA JSON over
`libvirt_qemu.qemuAgentCommand`, an unconditional `org.qemu.guest_agent.0`
virtio `<channel>`) ships with the libvirt rebuild (ADR-0008); Proxmox QGA,
VMware Tools, and Hyper-V PowerShell-Direct follow the same contract.

#### `qemu-guest-agent` is user-declared

For agent-based backends the guest needs the agent installed and running.
`CloudInitBuilder` is *not* changed to auto-inject it — that would be the
builder peeking at the communicator type. The plan author declares
`Apt("qemu-guest-agent")` + a `systemctl enable --now` line (see
`examples/native_agent.py`). A plan that forgets it fails at the first
`execute` with a clear `GuestAgentError`.

#### Error type

`GuestAgentError(DriverError)`. A brought-up VM whose agent never answers
surfaces here.

#### Where it lives (landed)

`testrange/guest_io.py` (shared Protocols, also §19), `exceptions.py`
(`GuestAgentError`), `drivers/base.py` (the `native_guest_*` accessors +
`native_guest_capabilities`), `drivers/mock.py` (reference transport),
`communicators/native.py` (`NativeCommunicator`), the orchestrator bind branch
in `run_phase`, plus `examples/native_agent.py` and unit coverage
(`test_native_communicator.py`, `test_mock_driver.py`, `test_drivers_base.py`).

## v0 example (target shape)

Canonical source: [`examples/hello_world.py`](examples/hello_world.py). Keep
that file as the authoritative shape — this section sketches the *structure*,
not a runnable copy. Per §19, plan-side tests do **not** carry a
`cloud_init_finished` probe; the orchestrator's `wait_ready` step handles
that.

```python
from testrange import Plan, OrchestratorHandle, run_tests
from testrange.cache import CacheEntry
from testrange.drivers.mock import MockHypervisor
from testrange.networks import Switch, Network
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.vms import VMSpec, VMRecipe
from testrange.builders import CloudInitBuilder
from testrange.credentials import PosixCred
from testrange.communicators import SSHCommunicator
from testrange.packages import Apt
from testrange.utils import SSHKey

_KEY = SSHKey.generate(comment="testrange-hello")

PLAN = Plan(
    MockHypervisor(
        build_uplink="eth0",
        networks=[
            Switch(
                "switch1",
                Network("netA"),
                cidr="172.31.0.0/24",
                uplink="eth0", mgmt=True, dhcp=True, dns=True, nat=True,
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="web",
                    devices=[
                        CPU(2), Memory(1024), OSDrive("pool1", 8),
                        NetworkIface("netA", addr=DHCPAddr()),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred("myuser", pubkey=_KEY.auth_line,
                                  privkey=_KEY.priv, sudo=True),
                    ],
                    packages=[Apt("nginx")],
                ),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)
```

Key shape invariants this demonstrates:

- `Switch(name, *networks, cidr=..., ...)` — networks are positional after the
  name; infra knobs (`cidr`, `uplink`, `mgmt`, `dhcp`, `dns`, `nat`) are
  keyword-only on the Switch (not on Networks). Per §10.
- `build_uplink="eth0"` on the Hypervisor — drives the transient build
  Switch's NAT path (§10, ADR-0010 §9).
- `SSHKey.generate(...)` returns `.auth_line` (single-line `authorized_keys`
  format) for `pubkey=` and `.priv` (OpenSSH PEM) for `privkey=`.
- `NetworkIface` is exported from `testrange.devices.network` (and re-exported
  from top-level `testrange.devices`), as are the address modes (`DHCPAddr`,
  `StaticAddr`). Backend-specific NIC subtypes, if a backend needs one, live
  under that backend's device module.

## v0 phases

Each phase has explicit state transitions so that an interrupted run can
be cleaned up via state-file-driven `testrange cleanup`.

1. **Pre-Flight** — read-only. Driver-side host checks (backend reachable,
   pool minimum-capacity floor, native-guest capability gap). Plan-side checks
   (subnet overlap, static-IP-out-of-CIDR, name uniqueness, singleton-device
   counts, CacheEntry resolvable). Returns
   `PreflightReport(errors, warnings)`. Errors abort; warnings advisory.

2. **Build** (was "Install" — renamed in ADR-0010) — per-VM, builder-driven,
   cache-aware. The cache is probed *before* any infra comes up:
   - Compute `builder.config_hash(...)` — deterministic 16-char hex, keying the
     whole **disk set** (OS + each data disk; ADR-0007/0010). Pure (no `run_id`).
     Folds the base image sha **and** the sidecar image sha (CI-1) — the build
     boots on the sidecar's network, so a drifted sidecar invalidates the set.
   - Probe each VM's full artifact set (`_built_<hash>__{os,dataN}`). Collect
     misses; only if ≥1 miss stand up the ephemeral build pool/switch/sidecar.
   - For each missing VM: push the base onto the VM's own OS disk + resize,
     `create_blank_volume` each data disk, render + attach the self-terminating
     seed, boot, **poll driver-level power-state** until shutoff, then
     `download_from_pool` every writable disk and `cache.add` each (push
     upstream if an HTTP tier is configured). Delete the build VM + disks.
   - At phase end tear down the build pool/switch/sidecar.
   - **All build resources recorded in state.json BEFORE create-call.**

   Communicators are not used during build. The builder owns the lifecycle
   end-to-end via its own seed plus driver-level power-state probes.

3. **Run**:
   - The run phase creates the user's declared networks and pools (build no
     longer leaves pools behind — ADR-0010 §9).
   - Sidecars are materialized and gated on readiness (native agent answers +
     config readback) before any user VM starts (ADR-0010 §8).
   - For each VM: push every cached built disk (OS + each data) onto the VM's
     own volume refs — no clone; define + start the run VM (no seed attached);
     communicator binds; `wait_ready` gates per-VM guest liveness.

4. **Test** — sequential, continue-on-failure default. Each test gets an
   `OrchestratorHandle` exposing `.vms[name]`, `.networks[name]`,
   `.pools[name]`, `.run_id`.

5. **Cleanup** — unless `--leak-on-failure` and any test failed:
   - Power off all VMs (graceful, then destroy on timeout).
   - Tear down VMs, networks, pools, in LIFO order from state.json.
   - Remove state.json on success.

## CLI surface (v0)

```
testrange --log-level {debug,info,warn,error}
testrange --cache https://… <subcommand>          # HTTP cache injection

testrange cache add <path-or-url> [--name <pretty>] [--description <text>]
testrange cache list
testrange cache del <hash-or-name>
testrange cache rename <hash-or-name> <new-name>
testrange cache forget-name <name>

testrange describe <plan.py>                       # passive; cache warnings only
testrange build <plan.py>                          # warm the cache; run NO tests
testrange run <plan.py>                            # auto-build on miss + tests + cleanup
testrange run --require-cache <plan.py>            # fail fast on a cache miss (no build)
testrange run --leak-on-failure <plan.py>
testrange run --fail-fast <plan.py>

testrange cleanup <run_id>
testrange cleanup --all
testrange cleanup --all --dry-run
```

Exit codes: 0 = success; 1 = test failure; 2 = preflight failure;
3 = cleanup failure; ≥ 64 = unexpected internal error.

## File layout

Reflects the tree as built (regenerated 2026-05-22). The device subpackages
collapsed to `base.py` per device (no `generic.py`/`libvirt.py` split — that
predated the multi-backend ABC and the libvirt deletion, ADR-0008). The
orchestrator is split into per-phase modules (the planned single `phases.py`).

```
docs/
    user/                       # user-facing guides (+ user/drivers/)
    dev/                        # contributor docs (+ dev/extending/)
    adr/                        # architecture decision records (0001–0010)
    index.md, conf.py           # Sphinx site
examples/
    hello_world.py  data_disk.py  native_agent.py
    network_modes.py  private_public.py
testrange/
    builders/
        base.py                 # Builder ABC (+ wait_ready)
        cloudinit.py            # CloudInitBuilder
        sidecar_iso.py          # sidecar config-ISO authoring
    cache/
        __init__.py  entry.py   # CacheEntry
        local.py                # LocalCache (file-based, sidecar JSON)
        http.py                 # HttpCache (best-effort)
        manager.py              # CacheManager (local + http tiers)
        _names.py
    communicators/
        base.py                 # Communicator ABC + ExecResult
        ssh.py                  # SSHCommunicator (paramiko)
        native.py               # NativeCommunicator (driver-agent shim)
    credentials/
        base.py                 # Credential ABC (pure data)
        posix.py                # PosixCred
    devices/
        base.py  __init__.py    # Device ABC + re-exports
        cpu/  memory/  disk/  network/  pool/   # each: base.py + __init__.py
    drivers/
        base.py                 # HypervisorDriver ABC
        mock.py                 # MockDriver + MockHypervisor (reference backend)
        _registry.py            # Hypervisor-type → driver dispatch
        proxmox/                # _client.py, _naming.py, _sdn.py (in progress)
    networks/
        base.py                 # Network, Switch, NetworkAddressing
        sidecar.py              # per-Switch dnsmasq/nftables sidecar render
        validate.py             # subnet/addressing validation
        _addressing_consts.py   # the .1/.2/.10–.99/.100–.254 pinning
    orchestrator/
        runtime.py              # Orchestrator, OrchestratorHandle, VMHandle
        context.py              # RunContext broker
        build_phase.py  build.py  run_phase.py  provision.py
        runner.py               # test runner (run_tests, build_range)
        teardown.py  artifacts.py
    packages/
        base.py  apt.py  pip.py
    state/
        store.py                # state.json + state.pid; atomic-rename writes
        schema.py               # version 1 dataclasses
        cleanup.py              # state-file-driven teardown (PID-checked)
    utils/
        sshkey.py               # SSHKey.generate
    vms/
        spec.py  recipe.py  handle.py
    cli.py  exceptions.py  guest_io.py  preflight.py  plan.py  _log.py
tests/
    unit/                       # 404 tests
    integration/                # gated by pytest mark
```

Stubs for proxmox / esxi / winrm are NOT exported until they work (no
Hyrum's-law re-exports of `NotImplementedError` shims).

## Implementation status (2026-05-22)

The v0 build-out and the ADR-0008 / ADR-0010 reshape are **landed**. The
step-by-step engineering-phase walkthroughs that used to fill this section
(v0 Phases 0-6 to a first green `hello_world`, then Build/Run Phases B0-B6 for
the install->build split) have been executed and are dropped; their decisions
live in the ADRs and the design sections above. Current state:

- **Suite green:** 404 unit tests pass; `ruff` + `mypy --strict` clean.
- **Reference backend:** `MockDriver` / `MockHypervisor` implement the full
  `HypervisorDriver` ABC (ADR-0008). The Proxmox driver is in progress on
  `feature/proxmox`; the libvirt driver is deleted pending a rebuild against
  the same ABC.
- **Build/run split (ADR-0010) complete:** `build_phase` warms the cache and
  nothing else; `run_phase` creates the user's pools, gates sidecar readiness,
  pushes every built disk (OS + each data disk) per VM, and runs tests.
  `testrange build` and `testrange run` (auto-build on miss; `--require-cache`
  to fail fast) are distinct CLI verbs.
- **Disks are cache artifacts:** `config_hash` keys the whole disk set;
  `create_blank_volume` + `resize_volume` replaced `create_disk_from_base`;
  data disks are built, cached, and restored.
- **Examples** cover the shapes: `hello_world`, `data_disk`, `native_agent`,
  `network_modes`, `private_public` — all on `MockHypervisor`.

The detailed phase history is recoverable from git (the ADR commits plus the
`wip(claude)` checkpoints). Forward-looking work lives in `TODO.md`.

### Deferred (named, not built)

- **Installer-based OS-disk origin** (ESXi Kickstart, Windows autounattend):
  blank OS disk + boot media, OS-disk origin behind a builder-owned method.
  Named in ADR-0010 §6; lands with the second builder and supersedes §6's
  image-based hard-coding. No abstraction built now.
- **Parallel build** of independent VMs (still sequential per ADR /
  decision 16).
- **Backend-side dedup / COW overlays** — explicitly rejected for v0 (§3);
  revisit only if redundant pushes become a measured bottleneck.
