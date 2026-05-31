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

### 10. Switch owns L2 topology; Sidecar owns the services (ESXi-shaped)

A Switch is one L2 broadcast domain and the place the *topology* decisions
live (`cidr`, `uplink`, `mgmt`). The *services* a sidecar VM serves at `.1`
(`dhcp`, `dns`, `nat`) are bundled into an optional `Sidecar` the Switch
carries — see [ADR-0013](docs/adr/0013-switch-sidecar-split.md). A Network is
a logical label (port-group) within a Switch — VMs attach by name. All
Networks on a Switch share `switch.cidr`; multiple Networks are
organizational labels on one wire.

```python
Sidecar(
    dhcp: bool = False,               # sidecar serves DHCP at .1
    dns:  bool = False,               # sidecar serves DNS at .1
    nat:  bool = False,               # sidecar MASQUERADEs out the uplink at .1
    addr: StaticAddr | None = None,   # NET-7: static sidecar eth1 (else DHCP)
)

Switch(
    name: str,
    *networks: Network,
    cidr: str = "192.168.10.0/24",   # strict network form; host-form raises
    uplink: str | None = None,        # physical NIC; testrange creates bridge(s)
    mgmt: bool = False,               # host adapter at .2 (NOT a router)
    sidecar: Sidecar | None = None,   # services at .1, or None for a bare wire
)
```

`sidecar=None` is the bare switch (pure L2 wire, incl. the pure-bridge mode:
`uplink=` with no sidecar). An all-off `Sidecar()` is forbidden — there is one
spelling of "no services". `needs_sidecar` is exactly `sidecar is not None`.

Validation splits along the concern boundary. **Intrinsic to the services**
(`Sidecar.__post_init__`): at-least-one-service; `addr` requires `nat=True`;
`addr` needs an explicit prefix. **Spanning both halves** (`Switch.__init__`):
`Sidecar(nat=True)` requires `uplink=` — the sidecar needs a physical NIC to
MASQUERADE out of, and the Switch is the only object seeing both topology and
services, so it owns that one cross-cutting rule.

`addr` (NET-7) pins the sidecar's MASQUERADE NIC (`eth1`) to a **static**
address + gateway + DNS instead of DHCP-from-the-out-of-band-egress-network. Use
it when that network won't DHCP the sidecar's MAC — e.g. a single-public-IP box
where `uplink` is an internal bridge the **host** NATs out its real NIC. The
default (no `addr`) is `eth1` DHCPs from the network behind the uplink, which is
the common case (ADR-0016 — egress is out-of-band; TestRange never manufactures
it). With a static uplink the sidecar's `eth1` never populates `resolv.conf`, so
its dnsmasq is pointed at `addr.dns` explicitly (`no-resolv` + `server=`).

**Addressing pinning** (`testrange/networks/_addressing_consts.py`):

- `.1` — sidecar (iff `sidecar is not None`); is the gateway when `nat`.
- `.2` — host mgmt adapter (iff `mgmt=True`).
- `.3`–`.9` — reserved.
- `.10`–`.99` — DHCP lease pool (iff the sidecar has `dhcp`).
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

There is no install-vs-run netplan split (ADR-0017). A build VM is given one
dedicated **build NIC** on the build switch (statically addressed from the
build switch's `.3` infra slot) in place of its declared NICs, and cloud-init's
`network-config` is a single match-by-MAC netplan covering the build NIC plus
every declared NIC. During build only the build NIC is present (so `apt`
egresses through it); at run only the declared NICs are (the build NIC's stanza
is inert) — the same baked file serves both phases. A
`99-testrange-disable-network.cfg` drop-in pins it across the seed-less run boot.

**Sidecar VM** (`testrange/networks/sidecar.py`,
`testrange/builders/sidecar_iso.py`): a pre-built Alpine image with
`dnsmasq`, `nftables`, and `qemu-guest-agent` baked in
(`tools/build-sidecar-image/build.sh`). Per-Switch instance —
materialized only when `switch.needs_sidecar` (= `switch.sidecar is not None`).
Per-run config is delivered as a tiny ISO9660 (label `TR_SIDECAR_CFG`)
carrying `dnsmasq.conf`, `interfaces`, `nftables.nft`, `sysctl.conf`.

**Named uplinks (ADR-0016).** `Switch.uplink` is a **logical name**, not a host
NIC. The bound connection profile carries an `[uplinks]` map (`name → host iface`)
that rides on the driver exactly like `backing_storage`; the driver resolves
`switch.uplink` → host iface inside `create_switch`. An unmapped name fails at
preflight (`unknown-uplink`). This keeps the last host-specific value out of the
portable plan. Egress is **out-of-band**: a named uplink is a host bridge the
operator provisions (NAT/DHCP behind it); TestRange never manufactures, SNATs, or
fences it — it only attaches.

**NAT topology** (`Sidecar(nat=True)` + `uplink="<named>"`): the driver realizes
TWO L2 segments — an isolated switch segment (guests + sidecar's eth0 at `.1`,
plus the host's `.2` if `mgmt`) and a separate uplink segment enslaving the
resolved host NIC (sidecar's eth1, DHCP from the out-of-band network behind it).
The sidecar's `nftables` ruleset MASQUERADEs eth0→eth1. Without a NAT sidecar, an
uplinked switch is one segment (guests bridge directly to the LAN with their own
MACs).

**Driver owns L2 (ADR-0008 §1):** the driver realizes the full topology for a
Switch via `create_switch(switch, backend_name)` (and the uplink-facing
segment when `switch.uplink and switch.sidecar` has `nat`); `create_network`
attaches a network to an already-created switch. The orchestrator never names
a bridge — it shuttles the logical uplink name in the `Switch` and the driver
resolves it; all bridge/vSwitch/SDN mechanics live inside the driver. No
backend-native NAT/DHCP/DNS anywhere — the sidecar owns those uniformly across
backends.

**Build phase** uses the same machinery, with a **user-declared** build switch on
the `Hypervisor` ([ADR-0016](docs/adr/0016-named-uplinks-out-of-band-egress.md)).
`build_switch: Switch | None` is portable topology (it references uplinks by
logical name, so it carries nothing host-specific), folded by
`resolve_build_switch` (`testrange/orchestrator/build.py`) into the transient
Switch the build phase brings up and tears down LIFO (ADR-0010 §9):

- `None` — an isolated `cidr="10.97.99.0/24"` switch, `Sidecar(dhcp=True,
  dns=True)`, **no uplink and so no egress**. A build needing apt/pip must
  declare a build switch (no magic default uplink).
- `Switch(...)` — honored as declared, **identical to a run-phase switch**: a
  bring-your-own uplink + sidecar (the sidecar may even be `None` for a builder
  carrying its own static L3). A NAT egress build switch is
  `Switch(uplink="<named>", sidecar=Sidecar(dhcp=True, dns=True, nat=True))`.

There is no managed/manufactured egress and no `supports_managed_build_egress`
capability — egress is the out-of-band uplink, same as a run switch.

**L2 realization is per-backend, daemon-mediated.** Each driver realizes the
fabric through its backend's own API — the libvirt driver defines a libvirt
`<network>` (the daemon builds the bridge, as root, on our behalf), Proxmox an
SDN vnet, ESXi a vSwitch. TestRange creates **no** host bridge itself: there is
no `pyroute2`/netlink path and no `CAP_NET_ADMIN` requirement (BACKEND-1, 2026).
Egress uplinks (`Switch.uplink`) resolve to a **pre-existing** host bridge the
operator provisions out-of-band; the driver only attaches to it.

**Known limits** (TODO.md): because libvirt's L2 is realized by the *daemon*,
a remote `qemu+ssh://` connection mostly works (the remote daemon builds the
bridge remotely) — the only constraint is that the named uplink bridge must
already exist *on the remote host* (BACKEND-5 reframed accordingly).
Multi-Network mgmt collapses naturally now that Switches own one CIDR.

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

### 16. Sync, single-threaded, single-instance (ADR-0002, ADR-0018)

Every dependency (libvirt-python, paramiko, requests, pycdlib) is
blocking. v0 runs single-threaded — install brings up one VM at a
time, tests run sequentially. No `asyncio`, no `ThreadPoolExecutor`.
Public API is sync.

**TestRange is single-instance (ADR-0018).** One `testrange` process runs
at a time per user and per driver profile. These are *unsupported* and not
guarded beyond the ownership check below: two invocations as the same user
(even different plans/backends — they share the local cache + state root);
the same plan run twice; two plans against the same profile. Run ranges
serially, or as separate users with distinct `XDG_STATE_HOME` /
`XDG_CACHE_HOME` roots and distinct profiles.

State-file safety is **crash safety, not concurrency**:
- Each `state.json` write is `.partial` + `os.replace` (atomic on every
  modern filesystem). This guarantees a SIGKILL / power loss mid-write
  leaves the file fully-old or fully-new for the single owning process —
  it does *not* serialize two writers, because by contract there is never
  more than one.
- A sibling `state.pid` file records the owning PID; `testrange cleanup`
  (the post-crash recovery tool) refuses to act on a run whose owner is
  still alive — that run's own `__exit__` owns its teardown. The mechanism
  hardens to an advisory `fcntl.flock` under CORE-30 (closing a PID-reuse
  window); cross-process `FileLock` for *live* concurrency stays declined,
  since under this contract there is no legitimate concurrent writer.

Multi-instance support is a deliberate future epic, tracked as **ORCH-10**
(one user, multiple different plans), **ORCH-11** (same plan twice), and
**ORCH-12** (different plans, same profile) — distinct from **ORCH-1**
(multiple `Hypervisor` entries in one plan) and **ORCH-4** (parallel build
*within* a single run), which are intra-process parallelism under a single
owner.

### 17. Cleanup-on-failure CLI flag: `--leak-on-failure`

Mutually exclusive with the future `--resume`.

### 18. Storage locations follow XDG semantics

- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.json` — run state.
- `$XDG_CACHE_HOME/testrange/isos/<sha>.bin` + `<sha>.json` —
  content-addressed cache.
- `$XDG_CACHE_HOME/testrange/staging/` — scratch for in-flight downloads and
  build-disk captures, kept on the cache filesystem so multi-GiB captures
  don't ENOSPC against a small tmpfs `/tmp` (CORE-4).

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
callables off the driver and hands them over. A backend that can't perform an
op leaves that `native_guest_*` accessor at its default (raises `DriverError`);
a per-op preflight capability gate is deferred until a backend actually lacks an
op (e.g. Hyper-V / WinRM) — see CORE-16.

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
(`GuestAgentError`), `drivers/base.py` (the `native_guest_*` accessors),
`drivers/mock.py` (reference transport),
`communicators/native.py` (`NativeCommunicator`), the orchestrator bind branch
in `run_phase`, plus `examples/native_agent.py` and unit coverage
(`test_native_communicator.py`, `test_mock_driver.py`, `test_drivers_base.py`).

### 21. Build result signaling: structured result over a universal serial sink

Today the build phase keys success on a single out-of-band bit:
`CloudInitBuilder` appends `poweroff` to `runcmd`, and `wait_for_shutoff`
(`orchestrator/build_phase.py`) polls power state until `shutoff` or
`build_timeout_s` (600s). That bit is both lossy and *wrong*:

- It cannot distinguish **succeeded** from **a command failed but the guest
  powered off anyway** — cloud-init `runcmd` runs under `sh` with no `set -e`,
  so a failed `apt update` does not abort the script and `poweroff` still
  runs. The orchestrator then **caches a corrupt post-install disk silently**.
- A failure that wedges the guest (cloud-init dies before user scripts) never
  powers off → the user pays the full `build_timeout_s` *and* gets **no
  diagnostic output**.

Two hard constraints on any replacement:

1. **Builder-agnostic.** cloud-init is one of several builders; the contract
   must also fit ESXi **Kickstart** (`%post`) and **Windows Unattended**
   (`SetupComplete.cmd`). The signaling contract lives *above* the Builder ABC.
2. **Must not require a native communicator.** Guests such as OpenBSD may ship
   no QGA / VMware-Tools agent (`native_guest_*` absent; preflight refuses to
   drive them — §20). So the result channel cannot depend on the communicator;
   the communicator is at most an optional accelerant.

**Decision: the guest reports an explicit, structured build result + captured
log over a serial console; the orchestrator reads that sink, treats the
positive token as the *only* success signal, and raises a typed
`BuildFailedError` otherwise.** Serial is the lowest-common-denominator
channel: a 16550 UART is the most portable virtual device there is — every
target guest OS writes to it (Linux `ttyS0`, the BSDs `com0`, Windows `COM1`,
the RHEL/ESXi installers `console=ttyS0`) and it is a property of the *virtual
hardware*, not of any in-guest agent, so it satisfies constraint 2.

#### The result protocol (builder-emitted, on the console)

Provisioning runs **fail-fast**. The guest emits framed records that survive
interleaving with boot chatter and tolerate binary payloads:

```
TESTRANGE-RESULT: ok
# --- or ---
TESTRANGE-RESULT: fail rc=100 cmd="apt-get update"
TESTRANGE-LOG-BEGIN
<base64 of the relevant log — the failing command's output, or a
 /var/log/cloud-init-output.log tail>
TESTRANGE-LOG-END
```

- **Success is the explicit `ok` token.** A guest that powers off *without*
  emitting `ok` is a failure (crashed mid-provision) — this is what kills the
  silent-corrupt-cache bug.
- On failure the guest emits the `fail` record (failing command + rc) and the
  framed log, then **powers off promptly** — so the failure path costs
  `boot + time-to-failing-command`, not `build_timeout_s`. The timeout reverts
  to a genuine watchdog for true wedges only.

#### Builder responsibility (per-dialect, uniform contract)

The Builder ABC gains the obligation to render provisioning that (a) runs
fail-fast, (b) emits the `TESTRANGE-RESULT:` record + framed log to the
console, (c) powers off. Each concrete implements it in its native dialect:

- `CloudInitBuilder` — wrap `runcmd` in a shell trap (`set -e`; on `ERR`, echo
  the `fail` record + base64'd `cloud-init-output.log` to `/dev/ttyS0`, then
  `poweroff`; on clean completion echo `ok` then `poweroff`). Replaces the bare
  `runcmd.append("poweroff")`.
- Kickstart (future) — `%post --erroronfail` (or a trap) echoing to
  `/dev/ttyS0`.
- Windows Unattended (future) — `SetupComplete.cmd` checks `errorlevel`,
  writes the record to `COM1`.

#### Orchestrator responsibility

`wait_for_shutoff` is replaced by `wait_for_build_result(ctx, backend, vm)`:

- Open the build-result sink via the new driver capability (below) right after
  `start_vm` and **live-tail** it concurrently with the build, short-circuiting
  the moment a `fail`/`ok` record arrives — real-time fast-fail. (A
  file-backed/degenerate sink polls instead of streams, but the orchestrator
  flow is the same.)
- Parse the record. `ok` → proceed to capture. `fail` /
  powered-off-without-token / watchdog-timeout → raise
  `BuildFailedError(vm, rc, cmd, log)`, decoding the framed log into the
  exception so the user sees *which command failed and its output*.
- Only on `ok` does `build_one_vm` proceed to `_capture_disk`.

#### Driver capability (hypervisor-level, not agent-level)

A new optional accessor on `HypervisorDriver` — provisionally
`read_build_result_sink(backend_name)` returning a **live byte-stream** the
orchestrator tails (a file-backed backend degenerates to polling; same caller
flow). It abstracts the per-backend *host read* of the serial console:

- **Proxmox** — read `serial0` live over **`termproxy`(POST) → `vncwebsocket`**
  (PVE-16: this is the *only* serial path — there is no REST GET for serial).
  proxmoxer issues the termproxy POST and holds the `PVEAuthCookie` ticket;
  a websocket client (`websocket-client`) consumes the stream
  (`vncwebsocket?port=&vncticket=`, first frame `user@realm:vncticket\n`),
  unwrapping the termproxy/VNC framing. **This is a second sanctioned transport
  exception beyond SFTP** — see the transport-policy note below. Requires
  password-ticket auth (termproxy rejects API tokens). The
  [[project_testrange_proxmox_transport]] disk fallback (ephemeral result disk
  over `download_from_pool`) stays *documented* in `RESEARCH.md` → "PVE-16
  spike" but is **not built** unless the websocket path proves unworkable.
- **libvirt** (future) — serial pty / unix-socket / file; live-tail.
- **Mock** — yields a canned stream; unit tests inject `ok` / `fail` records to
  drive both the success and the (currently untestable) failure path end-to-end
  on the reference backend.
- **ESXi / Hyper-V** (future) — datastore-file-backed serial / named pipe; the
  abstraction must not preclude these.

This is a hypervisor capability, distinct from `native_guest_*`; absence of
QGA does not affect it.

The vector is universal: every target OS writes the `TESTRANGE-RESULT:` record
to a UART (`/dev/ttyS0`, `COM1`, …), and every target backend can read that
serial console host-side (the mechanism differs — websocket on PVE, pty/file on
libvirt, datastore file on ESXi — but it's serial everywhere). So the builder
emits the record to the **serial console only**; the driver capability hides
the per-backend read.

**Transport-policy amendment (accepted 2026-05-24).** The Proxmox driver was
"proxmoxer-only, sole exception = SFTP `download_from_pool`". Reading serial
adds a **second** exception: a `vncwebsocket` connection on :8006 via
`websocket-client`. Accepted deliberately — it buys *live* fast-fail + live
build output that the disk fallback can't. Constraint: it forecloses moving the
driver to API-token auth (termproxy is password-ticket-only) unless serial
reverts to the disk fallback. Update ADR-0008 §6 + a new ADR for this decision.

#### Error type

`BuildFailedError(BuilderError)` carrying `vm`, `rc`, `cmd`, and the decoded
`log`. Distinct from `BuildTimeoutError` (the watchdog wedge case, which
stays). CLI maps it to a build failure with the captured log on stderr.

#### Files touched (landed 2026-05-24)

- `testrange/drivers/base.py` — `read_build_result_sink` accessor returning a
  `Generator[bytes, None, None]` (no bespoke sink type).
- `testrange/drivers/mock.py` — canned result-sink generator + test hooks.
- `testrange/drivers/proxmox/_client.py` + `_serial.py` — `serial0` reader over
  `termproxy`→`vncwebsocket` (new dep `websocket-client`; transport-policy
  amendment above), wired into `driver.py`.
- `testrange/builders/base.py` — the emit-result obligation on the ABC
  (serial console).
- `testrange/builders/cloudinit.py` — trap-wrapped `runcmd` emitting the
  record to `/dev/ttyS0`; drop the bare `poweroff`.
- `testrange/orchestrator/build_phase.py` — `wait_for_build_result` (live-tail)
  replaces `wait_for_shutoff`; capture gated on `ok`.
- `testrange/exceptions.py` — `BuildFailedError`.
- `pyproject.toml` — `websocket-client` under the `[proxmox]` extra.
- `tests/unit/` — `test_orchestrator.py` build-failure path (now reachable on
  the mock), `test_cloudinit.py` record rendering, driver serial tests.
- `docs/adr/` — new ADR (serial as the universal build-result vector) + amend
  ADR-0008 §6 for the second transport exception.

#### PVE sink — decided (PVE-16 spike + transport decision, 2026-05-24)

PVE reads `serial0` **live** over `termproxy`→`vncwebsocket` (`websocket-client`).
This was the spike's open question: serial-over-REST has no plain GET, so it
requires the websocket + a second transport — which the user accepted
(2026-05-24) for the live fast-fail / live-output payoff. The disk-over-SFTP
fallback remains documented (`RESEARCH.md` → "PVE-16 spike") but unbuilt. The
protocol, builder contract, orchestrator flow, and mock backing are
backend-independent and land first against the mock. Tickets: CORE-5
(capability + mock), BUILD-3 (emit contract), ORCH-6 (`wait_for_build_result` +
`BuildFailedError`), PVE-17 (serial-over-websocket reader).

#### Landed — backend-independent core (2026-05-24, ADR-0012)

CORE-5, BUILD-3, and ORCH-6 are **done** against the mock; PVE-17 (the Proxmox
`serial0` reader) is the remaining piece and is sequenced separately.

- **CORE-5** — `HypervisorDriver.read_build_result_sink(backend_name)` returns
  a `Generator[bytes, None, None]` (with the `b""` heartbeat contract so the
  orchestrator owns the watchdog); the orchestrator tails it under
  `contextlib.closing` so the driver's transport is released via the
  generator's `finally` even on an early break — no bespoke sink type. Default
  raises `DriverError("no build-result sink")`. `MockDriver` is the reference
  sink: canned `ok` by default; `build_result_stream` injects a `fail` /
  chatter stream and `build_result_wedge` emits heartbeats forever to exercise
  the watchdog.
- **BUILD-3** — the Builder ABC documents the emit-result obligation;
  `CloudInitBuilder` renders one fail-fast `["bash","-c", …]` `runcmd` entry
  (`set -eE` + `ERR` trap → framed `fail` record + base64 log tail on
  `/dev/ttyS0`; success → `sync` + `ok` + `poweroff`). **apt moved out of the
  `packages:`/`package_update:` directives into the trapped script** so a
  package failure is fail-fast.
- **ORCH-6** — `wait_for_build_result` (+ the backend-independent
  `parse_build_result` / `BuildResult`) replaces `wait_for_shutoff`; capture is
  gated on `ok`. `BuildFailedError(vm, rc, cmd, log)` is a `BuilderError`,
  surfaced by the CLI on stderr; `BuildTimeoutError` stays as the wedge
  watchdog. The orchestrator no longer polls `get_vm_power_state` during build.
  Console output is mirrored line-by-line to a dedicated `…build_phase.console`
  logger at DEBUG as it streams (framing suppressed), so a build is watchable
  live with `--log-level debug`.
- **CORE-6 (prerequisite, done)** — guest serial output is raw terminal output
  (ANSI/CSI colour + cursor escapes, OSC titles, embedded `\r`, C0 control
  bytes). `testrange/_ansi.py::scrub_terminal_control` strips it (keeping only
  `\n`/`\t`) at the two sinks that surface it to the operator: the live console
  mirror (`_ConsoleStreamer`) and the decoded `BuildFailedError` log. Without
  this the raw escapes hijack the operator's terminal (clear-screen/overwrite
  seen in live PVE runs) and garble the captured fail-log.
- **CORE-6 (`--verbose` live tail, done)** — `testrange/_tui.py::LiveTail` is a
  `logging.Handler` that renders streaming output as a Docker-BuildKit-style
  collapsing tail: a fixed-height ring-buffer region redrawn in place, with
  per-step collapse to a `=> build web  DONE 47s` summary; SIGWINCH-aware;
  cursor restored on teardown. Sources just *log* — the sink decides rendering:
  the `…console` (build serial) and `…runner.testout` (per-test stdout/stderr,
  teed via `capture_test_output`) loggers are the transient firehose, everything
  else on the `testrange` tree commits as a permanent line above the region.
  `_tui.live_output(verbose=…)` (entered by the CLI around `run`/`build`) is the
  TTY/non-TTY split: on a TTY it makes `LiveTail` the sole `testrange` handler
  for the run (so it and the plain stderr handler can't fight); off a TTY it
  bumps the console/testout loggers to DEBUG for plain per-line logging. The
  `--verbose` global flag composes with `--log-level` (verbose owns the TTY
  region; debug is the full firehose to the logger).

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
        build_switch=Switch(
            "build", Network("build"), cidr="10.97.99.0/24", uplink="eth0",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "switch1",
                Network("netA"),
                cidr="172.31.0.0/24",
                uplink="eth0", mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
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
- `build_switch=Switch(...)` on the Hypervisor — the user-declared transient
  build network (here a NAT Switch routing out a named uplink; omit for an
  isolated no-egress build). `uplink=` is a logical name resolved by the bound
  profile's `[uplinks]` map. §10, ADR-0016, ADR-0010 §9.
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
     seed, attach **one dedicated build NIC** on the build switch in place of the
     declared NICs (ADR-0017), boot, **read the serial build-result sink** (§21,
     ADR-0012) until the
     guest reports `ok`, then `download_from_pool` every writable disk and
     `cache.add` each (push upstream if an HTTP tier is configured). A `fail`
     record or a power-off without `ok` raises `BuildFailedError` *before*
     capture (no corrupt-disk caching); a true wedge trips `BuildTimeoutError`.
     Delete the build VM + disks.
   - At phase end tear down the build pool/switch/sidecar.
   - **All build resources recorded in state.json BEFORE create-call.**

   Communicators are not used during build. The builder owns the lifecycle
   end-to-end via its own seed; success is the explicit serial `ok` token, not
   a power-off (§21).

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
    network_modes.py  private_public.py  px_hello.py
    capabilities.py             # broad-coverage: every driver-facing feature
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

- **Suite green:** 434 unit tests pass; `ruff` + `mypy --strict` clean.
- **Reference backend (in transition):** `MockDriver` / `MockHypervisor`
  currently implement the full `HypervisorDriver` ABC (ADR-0008) and drive the
  unit suite. Per the **libvirt rebuild** (BACKEND-1, see *libvirt backend*
  below), libvirt becomes the **reference implementation** and the mock moves to
  `tests/` as a unit-only fixture — the mock simulates a backend but cannot run a
  real guest, so a live-certified driver is the better reference. The Proxmox
  driver is in progress on `feature/proxmox` (see *Proxmox backend* below).
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

### Proxmox backend (in progress, `feature/proxmox`)

Sequenced on the `ktui` board as the `PVE-*` series (`PVE-1`…`PVE-8` built;
`PVE-9`…`PVE-24` the live-shakeout/finish work, **now closed** — see *Open work*
below). **Green end-to-end as of 2026-05-24** (PVE-9): a full `testrange run` of
`examples/px_hello.py` (debian + nginx, reached over QGA) built and ran to green
against the live host
(PVE **9.2.2**, single node `ns1001849`, one `dir` storage `local`, SDN present
but empty).

**Landed:**

- **`PVE-1` — driver keystone.** `ProxmoxHypervisor` (the Plan entry: `host`/
  `node` + connection config + `networks`/`pools`/`vms`/`build_switch`) and
  `ProxmoxDriver`, assembled in `drivers/proxmox/driver.py` and registered. It
  delegates to the concern modules `_client` (proxmoxer REST + lazy paramiko
  SFTP), `_naming` (pure deterministic names), `_sdn` (L2). `connect`/
  `disconnect`, plan-side `preflight`, the naming surface, and L2 are
  implemented; storage (`PVE-3`), VM lifecycle (`PVE-8`), the native agent
  (`PVE-4`), and snapshots (`PVE-5`) raise `DriverError("PVE-x …")` until their
  tickets land (the ABC is all-abstract, so the concrete must be instantiable).
- **`PVE-2` — L2 via SDN, incl. the uplink path.** The isolated guest segment
  is a per-Switch SDN `vnet` in a `simple` zone (staged, then a single
  `PUT /cluster/sdn` to apply); networks share the switch's vnet.
  `destroy_switch` is self-discovering so a `from_uri`-rebuilt teardown driver
  needs no `run_id`. For an `uplink+nat` Switch, `create_switch` returns the
  **existing host bridge** that `switch.uplink` resolves to via the profile's
  named-uplink map (ADR-0016; e.g. `vmbr0`, the one carrying the default
  gateway) as the segment the sidecar's `eth1` rides — so on Proxmox an `uplink`
  names an existing bridge, not a raw NIC, and `testrange run` **auto-builds**
  end-to-end. Preflight verifies the bridge exists
  (`proxmox-uplink-bridge-missing`). Egress is **out-of-band** (ADR-0016): the
  operator owns the uplink bridge's NAT/DHCP and the driver only attaches to it
  — there is no manufactured `snat` vnet or VNet-firewall fence. The SDK is
  lazily imported, so the package registers with no `proxmoxer` installed; unit
  tests inject a duck-typed fake client.

- **`PVE-6` — stamped-name → vmid resolution.** `create_vm` stamps the composed
  backend name into the VM's PVE `name`; `_vm.resolve_vmid` recovers the vmid by
  scanning the node's guest list (no external map; a `from_uri` teardown driver
  recovers handles unaided — ADR-0008 §6).
- **`PVE-8` — VM lifecycle (`_vm`).** `create_vm` (import-from OS → `scsi0`, grow
  to spec when a seed is present; blank vs import data disks; seed `ide2` CDROM;
  `net<i>` on the backend bridge/vnet with stable MACs; `agent=1`) + start /
  graceful-then-forced shutdown / stop+purge destroy / power-state (PVE
  `stopped`→`shutoff`). **Live-validated** end-to-end against the host through
  proxmoxer (upload→create→resolve→start→destroy). All proxmoxer; REST-native
  sizing (`qm resize`), no `qemu-img`.
- **`PVE-3` — pool + volume I/O (`_storage`).** Pools are a filename-prefix
  namespace inside the static `dir` storage (no PVE object). Transport: the
  control plane is proxmoxer; **volume bytes ride SFTP both directions** —
  `upload_to_pool`/`write_to_pool` `sftp_put` into the storage content dir
  (`import/` for disks, `template/iso/` for seeds), `download_from_pool`
  `sftp_get`s. PVE's REST has no volume byte-egress *and* its `upload` endpoint
  501s on large `import` images (PVE-23), so bytes don't ride REST (ADR-0008
  §6). A `dir`/`nfs` store discovers an SFTP-dropped file by scan → the same
  volid. The **disk model is "Option-2"**: the
  orchestrator's one stable `VolumeRef` is the *staging* content volume for
  `upload`/`write`/`delete`, but `download_from_pool` **re-resolves** it to the
  live vm-scoped disk (`_vm.resolve_disk` → the VM's config `scsiN` volid) so it
  captures what the build VM actually wrote, not the stale pre-boot upload.
  `create_blank_volume`/`resize_volume` defer to `create_vm` (which has the spec
  sizes and the vmid). Preflight requires the `import` content type
  (`proxmox-import-content-missing`).
- **`PVE-4` — QGA native transport (`_guest`).** `agent/exec` (pid + poll
  `exec-status`), `file-read` (→ bytes), `file-write` (binary-safe via base64 +
  `encode=0`, single-write cap raises). Makes all three `native_guest_*`
  accessors live, unblocking `NativeCommunicator` + sidecar DHCP-lease readback.
- **`PVE-5` — snapshots (`_vm`).** create (`vmstate=1` for memory), list
  (excludes PVE's synthetic `current`, oldest-first), delete (no-op if absent),
  rollback; duplicate/missing raise `DriverError`. **Live-validated.**
- **`PVE-7` — integration suite.** `tests/integration/test_proxmox.py`, marked
  `proxmox` (excluded from the default gate), gated on `TESTRANGE_PVE_HOST` and a
  base qcow2; self-cleaning. **Ran green against the live host** (connect/
  preflight, SDN round-trip, storage upload/delete, full VM lifecycle+snapshot).

The Proxmox backend is **green end-to-end** (PVE-9, 2026-05-24): the first full
`testrange run` surfaced a cluster of integration bugs (PVE-10…13, PVE-15, plus
the build-result/serial work PVE-16…18 and the author-surface/realm/SFTP fixes
PVE-20…24, ORCH-7, NET-7) — **all now fixed, unit-tested, and confirmed live**.
The QGA *wire* (`PVE-4` exec/file ops) and the serial build-result sink
(`PVE-17`) were exercised for real by that run (the run reaches the guest over a
`NativeCommunicator` and keys build success on the serial `TESTRANGE-RESULT`
record).

**Scope of what is validated live vs. still unexercised.** The green run covered
the *minimal* topology — one VM, one NIC, one Switch, no data disks, disk-only
snapshot. The single-VM happy path is solid; the paths most likely to break next
are multi-VM, multi-NIC, multi-data-disk (the `scsi<i+1>` + Option-2
`resolve_disk` longest-match), multi-Switch (per-Switch vnet sharing the per-run
zone + zone-GC on the last `destroy_switch`), `mem=True` snapshots, and
**multi-node clusters** (node-scoping is currently baked in — single-node is the
validated target). A fresh-eyes review of the driver (2026-05-24) also surfaced a
handful of robustness gaps now filed as PVE-25…34 (see *Open work*). None block
the single-node lab use case; they harden it.

**Decisions made here:**

- **proxmoxer for the control plane; volume bytes + serial are out-of-band.**
  The driver goes through the proxmoxer REST API for everything PVE does over
  REST. Three things can't ride REST and use sanctioned side channels: (1) **all
  volume bytes over paramiko SFTP, both directions** — REST has no byte-egress
  *and* its `upload` endpoint 501s on large `import` images (PVE-23), so
  `sftp_put`/`sftp_get` write/read the file under the storage content dir; (2)
  the **`vncwebsocket`** serial read for the build-result sink (no REST GET for
  serial — PVE-17, ADR-0012). All three are ADR-0008 §6. No `subprocess`/no
  `qemu-img`: disk sizing stays REST-native (`qm resize`, `scsiN=<storage>:<size>`).
- **Disk lifecycle is "Option-2" (stateless re-resolution).** PVE allocates a
  vm-scoped volid at `create_vm` (often via a *copying* `import-from`), so a ref
  can't denote the same bytes before and after create. Rather than hold an
  in-process ref→volid map, `download` re-derives the live disk from the VM's
  stamped name each call — survives a process restart and keeps crash-teardown
  correct (`destroy_vm` purges the disks). Heavily documented in `_storage` /
  `_vm` at the user's request.
- **SDN zone is per-run and fully TestRange-managed (PVE-20).** The driver mints
  a `tr<hex>` zone (8 chars — PVE's SDN-id limit) once per instance (one driver
  == one run), creates it on first `create_switch`, and self-discovers + drops
  it on teardown. It is **not** an author knob (TestRange owns its lifecycle) and
  needs no determinism (a `from_uri` teardown driver reads the zone off the vnet,
  not from `run_id`). Per-run rather than a shared fixed zone avoids cross-run
  commingling and is the shape multi-run will need.
- **`dir`/`nfs` storage only**, so `compose_volume_ref` stays
  filename-deterministic (ADR-0008 §6).
- **Author surface is `host`/`user`/`password` kwargs + sane defaults (PVE-20,
  revised PVE-22).** `ProxmoxHypervisor(host="10.0.0.5", password="…",
  networks=…, pools=…, vms=…)` is the whole common case: `user` defaults to
  `root@pam` (a bare `"root"` is normalised to `root@pam` — PVE's realm, PVE-21),
  `node` auto-detects the single node at connect, `build_switch` defaults to
  `None` (isolated build network, no egress — ADR-0016), `backing_storage` to
  `local`, SSH reuses the API creds. The
  operational knobs stay optional. (A connection-URI surface was tried and
  dropped — the `@realm` + special-char-password escaping made plain kwargs the
  better fit.) The `proxmox://` URI survives only as the *internal* teardown
  serialization: the orchestrator persists the resolved `hyp.driver_uri`
  (storage/ssh/node) into state for `cleanup`.

**Open work (board, as of 2026-05-24).** The PVE-9 live smoke test surfaced an
integration-bug cluster; **the cluster is now closed and PVE-9 is green**. The
remaining PVE work is hardening + breadth, filed as PVE-25…34, none blocking the
single-node lab use case. The first review cluster (PVE-25/26/29) is **done +
unit-tested** (2026-05-24); the rest are in *Ready*:

- **`PVE-25`** *(bug, done)* — `upload_to_pool` now checks existence first and
  skips the re-upload, honoring the ABC idempotency contract (no multi-GB
  re-transfer on retry/resume).
- **`PVE-26`** *(bug, done)* — `content_volume_exists` (lists + tests membership)
  replaces the swallow-all probe; `delete_volume` establishes absence by that
  check and lets a real present-volume delete error propagate (teardown no longer
  forgets+leaks on a genuine failure).
- **`PVE-27`** *(bug/design, done)* — `create_vm` decides build-vs-run data disks
  from the orchestrator's intent (seed presence) instead of a backend probe, so a
  stale staging volume from a crashed build can't be mis-imported. (Revisit when
  installer-based OS origins land — BUILD-1 — which may carry no seed.)
- **`PVE-28`** *(re-verified: not a bug)* — the claimed `_resize_os_disk`
  double-resize doesn't occur (the `wait_task` poll-timeout message contains no
  `timeout`/`lock` substring, so it's never classified transient). Residual LOW
  nit: the classification matches free-form text rather than the task exitstatus.
- **`PVE-29`** *(bug, done)* — serial sink: a keepalive-send failure now raises a
  typed transport error instead of looking like a clean poweroff, and an empty
  frame yields a `b""` heartbeat instead of busy-spinning the watchdog.
- **`PVE-30`** *(bug, done)* — plan validation now reserves the `-data<N>` marker
  in VM names (the collision class where a VM is named like another VM's data
  disk and they share one volume ref); backend-agnostic guard in `validate.py`.
- **`PVE-31`** *(feat, backgrounded — design pending ADR)* — multi-node clusters.
  Concluded to be **two** concepts split by where the connection lives: a *native
  cluster* (PVE cluster, vCenter — one endpoint, internal hosts, shared SDN/
  storage) is a **driver-internal placement seam**, not a Plan type; a
  *federation* (`Cluster(*hypervisors)`, N endpoints, no shared L2) is ORCH-2's
  `AbstractHypervisor`. v1 scope: PVE is **single-node**; the near-term extract is
  a single-node preflight guard. Needs an ADR fixing the scope + the split.
- **`PVE-32`** *(test)* — live coverage beyond the minimal smoke (multi-VM /
  -NIC / -data-disk / -switch / mem-snapshot). **The feature-complete gate.**
- **`PVE-33`** *(feat)* — block-storage backends (lvm/zfs/ceph); today `dir`/`nfs`
  only, failing loud on a block store. Out of v1 scope.

Closed in this cluster (fixed + unit-tested, confirmed by the green run):

- **`PVE-9`** — *done 2026-05-24: green end-to-end.* The full `testrange run` of
  `examples/px_hello.py` built (sidecar NAT over a host-NAT internal bridge —
  build-egress caveat below) and ran to green over QGA. Exercised SDN L2, SFTP
  upload, import-from OS disk, the serial build-result sink (PVE-17), and the QGA
  wire (PVE-4) against the live host.
- **`PVE-10`** — *done.* proxmoxer's 5s default request timeout aborted
  multi-hundred-MB uploads → session `timeout=600s` (`test_connect_uses_generous_http_timeout`).
- **`PVE-11`** — *done.* `create_vm` wired `net<i>` to the composed network
  *name*, but a PVE NIC needs the SDN *vnet id* → composed-name→vnet-id map in
  `create_network`, translated in `create_vm` (`test_create_vm_translates_nic_bridge_to_vnet_id`).
- **`PVE-12`** — *done.* `create_vm`'s import-from + resize hold the config
  `lock`; the immediate `start_vm` raced it → `_wait_unlocked` polls the lock and
  `_resize_os_disk` retries the transient file-lock race (`test_create_waits_for_config_lock_to_clear`).
- **`PVE-13`** — *done.* without `requests-toolbelt` proxmoxer buffers uploads and
  caps at 2 GiB → added to the `[proxmox]` extra.
- **`PVE-15`** — *done.* large transfers went silent → `ProgressReporter`
  (`_progress.py`) + actionable slow-host `DriverError`; `_ProgressFile(io.IOBase)`
  keeps proxmoxer streaming (no OOM). `test_progress.py`.
- **`PVE-17`** — *done.* the build-result sink reads `serial0` live over
  `termproxy`→`vncwebsocket` (`websocket-client`): `_client.open_serial_websocket`
  + `_serial.read_build_result_sink` (the `Generator[bytes]` the orchestrator
  tails — raw PTY frames, `b""` heartbeat + `"2"` keepalive on idle, closes on
  exit). Second sanctioned transport (ADR-0008 §6 amended, ADR-0012). Faked-ws
  unit tests; live exercise rides PVE-9.
- **`NET-1`** — *done.* `validate.py`'s DHCP-pool hint now derives from
  `USER_STATIC_LO/HI` instead of hardcoded `+100/+254`.

**Build egress (out-of-band — ADR-0016, supersedes ADR-0014 / NET-11).** A
hosting LAN may refuse the sidecar's extra MAC for DHCP egress (confirmed live on
a single-public-IP OVH-style box: the host's own IP egresses fine, but the
sidecar's MAC gets no lease). TestRange does **not** manufacture or fence egress.
Instead, `Switch.uplink` is a logical name the profile's `[<name>.uplinks]` map
resolves to an operator-provided bridge/network that already NATs/DHCPs out; the
driver only attaches the sidecar's `eth1` to that resolved uplink. Standing up
the NAT bridge is a one-time out-of-band operator step, documented per backend in
`docs/user/drivers/out-of-band-egress.md`.

### libvirt backend (full rewrite in progress, BACKEND-1)

The pre-existing libvirt skeleton (connection/naming/profile/preflight + the
`_todo()`-gated surface) is being **rewritten from zero** against the current
ABC — libvirt becomes the **reference implementation** and the mock retires to
`tests/`. Sequenced on the board as `BACKEND-1.0`…`1.E`. The earlier BACKEND-1.1
slice and its managed-egress framing (pyroute2 bridges + `nwfilter` fence +
`supports_managed_build_egress`) are **superseded** — by ADR-0016 (egress is
out-of-band) and by the decisions below.

**Decisions (proven on the dev host, non-root, 2026-05-30):**

- **`libvirt-python` is the only libvirt dependency. `pyroute2` is dropped.** L2
  is realized through the libvirt **network API** (`networkDefineXML` /
  `networkCreate`), not netlink. The daemon builds the bridge; we never hold
  `CAP_NET_ADMIN`. (`pyroute2` removed from the `[libvirt]` extra and `_conn`.)
- **No root, no pre-install step. Membership in the `libvirt` group is the only
  requirement.** Verified end-to-end as uid 1000: `qemu:///system` connect; pool
  `defineXML → build → create`; qcow2 `vol create`; stream `upload`/`download`;
  full teardown; isolated-network define/undefine. `pool.build()` makes the
  target dir under the root-owned `/var/lib/libvirt/images` *as the daemon*, so
  no host directory has to be pre-created or made user-writable.
- **Per-run storage pools, driver-created.** The driver creates a `dir` pool per
  run (target under `/var/lib/libvirt/images/tr-<run8>-<pool>`) on run start and
  tears it down on cleanup (`destroy → delete → undefine`), exactly the
  `create_pool` / `destroy_pool` lifecycle the orchestrator already drives for
  the mock and Proxmox. **No pre-existing pool dependency; the `backing_pool`
  profile knob is removed.** Volume bytes ride the libvirt **stream API**
  (`virStorageVol.upload/download`) both directions — qemu-readable at VM-start,
  no host-file ownership games, no `qemu-img` (subprocess ban holds).
- **`LibvirtProfile` shrinks to `uri` (+ the named-uplink map).** `backing_pool`
  gone.
- **Egress uplink is a pre-existing host bridge** (`tr-egress`, *not* libvirt's
  `default`/`virbr0`), provisioned out-of-band as a libvirt NAT network whose
  built-in `dnsmasq` serves DHCP and whose `forward mode='nat'` masquerades out
  the host's real NIC. Mapped `egress = "tr-egress"` in the profile; the driver
  only attaches the sidecar's `eth1`. Recipe in
  `docs/user/drivers/out-of-band-egress.md`.

**Build sequence (thin vertical slice → green, then widen):**

- **1.0** — rip out `drivers/libvirt/*` + `test_libvirt_*`; drop `pyroute2`;
  lay the concern-module skeleton (`_conn _naming _profile _net _storage _vm
  _guest _serial driver.py`).
- **1.A** — storage: per-run pools + stream volume I/O (TDD against a faked
  `LibvirtClient`).
- **1.B** — VM + serial build-result sink + QGA, **no network**: domain XML
  (qcow2 disks, stable-MAC NICs, `<serial type='unix'>`, an
  `org.qemu.guest_agent.0` virtio channel, seed CD-ROM); lifecycle; the serial
  sink live-tail; `native_guest_*` over `libvirt_qemu.qemuAgentCommand`. First
  real end-to-end green: a no-net VM builds (serial `ok`) and runs over
  `NativeCommunicator`.
- **1.C** — L2 via libvirt networks: isolated segment + NAT uplink segment onto
  `tr-egress` + mgmt `.2`; sidecar boot/readiness → DHCP-lease discovery via the
  native guest transport. **`examples/hello_world.py` green.**
- **1.D** — widen to certification: snapshots (disk + memory), data disks,
  static/unmanaged NICs, password users. **`testrange run --profile
  libvirt-local examples/capabilities.py` green + `pytest -m libvirt` green, as
  plain `user`.** Adds `tests/integration/test_libvirt.py`.
- **1.E** — move `drivers/mock.py` → `tests/`, register the `mock` scheme in
  `tests/conftest.py`, drop its side-effect import from `drivers/__init__.py`;
  ADR + docs (install/connecting/extending, the `tr-egress` recipe).

**Status (2026-05-30): 1.0–1.D driver code complete and live-certified.**
`testrange run --profile libvirt-local examples/{hello_world,capabilities}.py`
are **green** on real `qemu:///system` as a plain `libvirt`-group user (no root).
Three non-obvious realities surfaced by being the first backend to *really*
execute the build/run, all now baked into the driver:

- **A headless domain still needs a `<video>` device.** Under libvirt's
  `-nodefaults` there is no implicit VGA, and the Debian cloud image's GRUB
  `gfxterm` loops forever ("Booting `Debian GNU/Linux'") with no adapter — the
  kernel never starts. The domain XML carries `<video><model type='vga'/></video>`
  (no `<graphics>` backend; we still drive the console over the serial sink).
- **The serial build-result sink is `mode='connect'`, not `mode='bind'`.** A
  qemu-owned bind socket (`0775 libvirt-qemu`) is not connect-able by uid 1000,
  so the driver *listens* (socket pre-bound in `create_vm`, under a `/tmp`-rooted
  0755 dir the daemon can traverse) and QEMU connects in. Run VMs get a throwaway
  `<serial type='pty'>` (nothing to drain).
- **`Switch(mgmt=True)` is implemented (the `.2` host adapter); the network is
  otherwise fully isolated.** A non-mgmt Switch's network has no host `<ip>` — it
  is a pure guest segment (the host is not on it). A `mgmt=True` Switch adds the
  `.2` host adapter to the bridge (`<ip address='…2'/>` + `<dns enable='no'/>` so
  libvirt spawns no dnsmasq to shadow the sidecar) — and *only that* (it is not a
  router). That `.2` adapter is how the on-host orchestrator reaches an
  `SSHCommunicator` VM on a local network, so libvirt drops the
  `mgmt_unsupported_findings` gate (ADR-0009's "a backend that grows real mgmt
  support drops the call"); other backends keep it pending the ADR. A plan that
  uses `SSHCommunicator` on an isolated, non-mgmt Switch is a plan-authoring
  issue, not something the driver papers over — so the examples declare
  `mgmt=True` on the Switches whose VMs are reached over SSH.
- **Snapshots are full internal qcow2 snapshots.** libvirt rejects an internal
  *disk-only* snapshot of a *running* domain, so `create_snapshot` always takes a
  full checkpoint (libvirt includes RAM while running, disk-only while shut off);
  `mem` is accepted for ABC parity. Disk-revert and memory-restore both verified
  live.

**Certification status.** `testrange run --profile libvirt-local
examples/capabilities.py` is **green** (30/30 enabled tests) and
`tests/integration/test_libvirt.py` (marked `libvirt`, self-cleaning) passes
**3/3** live — that integration suite is the authoritative driver cert. The mock
backend has moved to `tests/mock_driver.py` (registered by `tests/conftest.py`);
libvirt is the reference implementation. **Remaining (1.E):** the reference-impl
ADR + docs (install/connecting/extending, the `tr-egress` recipe).

The certification surfaced a set of **capabilities-test / orchestrator** issues
(none a libvirt-driver defect). The test-authoring ones were fixed in
`capabilities.py`: `Apt("python3-pip")` before `Pip(...)`; `resolvectl status`
instead of grepping the systemd-resolved `/etc/resolv.conf` stub; `sudo blkid`
(raw-device read needs root, the SSH user is not); the RAM-restore marker in
`/dev/shm` (tmpfs + world-writable) rather than root-only `/run`. The one real
orchestrator gap it surfaced — a zero-NIC VM getting no build-time network, so
its `apt`-based builder couldn't run — was **fixed by ADR-0017** (every build VM
gets a dedicated transient build NIC); `no-net` is re-enabled in `capabilities.py`.

### 22. Backend binding: topology Plan entry vs. resolved backend

A Plan entry used to be a concrete `*Hypervisor` that conflated four jobs:
topology, backend selection (its type drove `driver_for`), connection, and
environment knobs (build egress / backing storage / node). That pinned every
test to one backend and put a host address + password in the committed plan.
ADR-0015 splits them.

- **Generic `Hypervisor`** (`from testrange import Hypervisor`) — portable
  topology only (networks/pools/vms). Unregistered in the driver registry, so
  it selects no driver and carries no connection. The entry a portable plan uses.
- **Concrete `*Hypervisor`** (CORE-19) — an empty subclass of `Hypervisor` plus
  a registered scheme marker. Topology-only, carries no connection; its only
  job is to assert *this topology MUST run against backend X* so a mismatched
  `--profile` is caught at preflight (e.g., a PVE-specific CPU type).
- **Connection profile** (`testrange/connect.py`) — a local TOML file selected
  with `--profile [file:]name` (CORE-22, multi-profile). Names the driver by
  *scheme* (`driver = "proxmox"`), carries the connection (inline plain-string
  password; lab posture), and carries the named-uplink map (`[<name>.uplinks]`,
  ADR-0016) that resolves a Switch's logical `uplink` to a host bridge/network.
  No env-var fallback; `.gitignore` keeps a real profile TOML out of git.
  `BackendProfile` is an ABC (CORE-18); each backend ships a concrete subclass
  (`ProxmoxProfile` / `LibvirtProfile` / `MockProfile`) that self-registers its
  scheme and builds its own driver via `profile.build_driver()`.

`resolve_backend(plan, profile)` (`testrange/orchestrator/backend.py`) folds the
entry + required profile into `ResolvedBackend { driver, driver_uri }`, which the
orchestrator reads instead of reaching into `plan.hypervisor` for the driver/uri.
The build switch is **not** part of the resolved backend — it is portable
topology on the Hypervisor (`build_switch: Switch | None`, ADR-0016), read
directly from `plan.hypervisor`. The matrix (CORE-19 collapse — both "+none"
cells are hard errors now):

| (entry, profile) | resolution |
| ---------------- | ---------- |
| concrete + none  | hard error: pinned scheme, no connection. Names the scheme so the dev knows which profile to point `--profile` at. |
| concrete + given | profile `scheme` **must** equal the entry's scheme (else hard error); `driver = profile.build_driver()`. |
| generic + none   | hard error: backend-agnostic; pass `--profile`. |
| generic + given  | `driver = profile.build_driver()`. |

Compatibility preflight is three layers: (1) the scheme-pin/profile-match
above, raised in `resolve_backend`; (2) `compatibility_findings(plan, driver)`,
merged by the orchestrator before driver preflight; (3) the resolved
driver's own live `preflight`. `RunContext.resolved` holds the binding;
`ctx.driver` is a property over it. The driver registry carries name dispatch
(`driver_for_name`, cleanup) + `scheme_for_hypervisor` / `is_pinned` (pin
introspection); the type-driven `driver_for` path is gone (CORE-19 — the
concrete+none cell that needed it now errors).

### Deferred (named, not built)

- **Installer-based OS-disk origin** (ESXi Kickstart, Windows autounattend):
  blank OS disk + boot media, OS-disk origin behind a builder-owned method.
  Named in ADR-0010 §6; lands with the second builder and supersedes §6's
  image-based hard-coding. No abstraction built now.
- **Parallel build** of independent VMs (still sequential per ADR /
  decision 16).
- **Backend-side dedup / COW overlays** — explicitly rejected for v0 (§3);
  revisit only if redundant pushes become a measured bottleneck.
