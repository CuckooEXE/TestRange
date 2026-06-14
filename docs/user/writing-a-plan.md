# Writing a Plan

A `testrange` plan is a Python file that declares a top-level
``PLAN = Plan(...)`` and a ``TESTS = [...]`` list. The CLI imports
the file and uses both. You build the topology imperatively — register
pools, switches, and VMs on a `Hypervisor`, referencing them through the
typed handles registration returns — and ``Plan(...)`` freezes the result
into a validated build graph.
[Thinking in build graphs](thinking-in-build-graphs.md) is the conceptual
on-ramp for that model; this page is the practical reference.

```{note}
A plan's Hypervisor entry can be **portable** (the generic `Hypervisor`, which
takes its backend from a `--profile` connection profile) or **pinned** (a concrete
`*Hypervisor` like `ProxmoxHypervisor`). See
[Connecting to a backend](connecting-to-a-backend.md) for the split; this page
covers the topology the entry carries either way.
```

## Minimal plan

```python
from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.utils import SSHKey
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt

# Deterministic from `comment`; same comment -> same keypair across runs,
# which keeps the rendered cloud-init seed byte-stable so the build
# cache hits on subsequent invocations. Insecure by design; test-only.
_KEY = SSHKey.generate(comment="hello")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

pool1 = hyp.add_pool(StoragePool("pool1", 32))

hyp.add_switch(
    Switch(
        "sw1",
        Network("netA"),
        cidr="10.0.1.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
netA = hyp.networks["netA"]

hyp.vm(
    "web",
    cpu=CPU(2),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(netA, DHCPAddr())],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("alice", ssh_key=_KEY, admin=True)],
        packages=[Apt("nginx")],
    ),
    communicator=SSHCommunicator("alice"),
)

PLAN = Plan("hello", hyp)

def nginx_is_running(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["systemctl", "is-active", "nginx"])
    assert r.exit_code == 0, r

TESTS = [nginx_is_running]
```

Then:

```sh
testrange cache add https://cloud.debian.org/.../debian-13-generic-amd64.qcow2 \
    --name debian-13
testrange describe path/to/plan.py --profile libvirt-local
testrange graph path/to/plan.py --order
testrange run path/to/plan.py --profile libvirt-local
```

The generic `Hypervisor` above takes its backend from the `--profile` connection
profile; see [Connecting to a backend](connecting-to-a-backend.md).

## Handles, edges, and the frozen graph

Four rules govern the construction surface:

- **`add_pool` / `add_switch` register a node and return its typed handle**;
  every registered node is also reachable through the typed registries
  `hyp.pools` / `hyp.networks` / `hyp.switches` / `hyp.vms`. Capture the handle
  in a local and reference it directly — `pool1 = hyp.add_pool(...)` then
  `OSDrive(pool1, 8)`; `netA = hyp.networks["netA"]` then `NetworkIface(netA,
  ...)`. Devices take handles, never strings: a typo'd name is a loud `KeyError`
  at construction (listing the names that exist), and a wrong handle kind (a
  network where a pool belongs) fails both mypy and the constructor.
- **`hyp.vm(name, *, cpu, memory, os_drive, builder, communicator, nics=(),
  data_disks=())` adds a VM** and returns its `VMHandle`. The exactly-one
  devices are named params (so swapping a slot is a mypy error) and the
  zero-or-more devices are the `nics` / `data_disks` sequences. It is sugar over
  the explicit form `add_vm(VMRecipe(spec=VMSpec(devices=[...]), builder=...,
  communicator=...))`, which stays available as the escape door for shapes
  `vm()` does not model (for example a backend-concrete device variant, or a
  parameterized recipe built by a helper — see
  `tests/plans/generic/concurrency.py`).
- **Every handle reference becomes a dependency edge** (a VM depends on its
  pool and its switch). `.needs()` adds the ordering the executor cannot
  infer:

  ```python
  db = hyp.vm("db", cpu=CPU(1), memory=Memory(512), os_drive=OSDrive(pool1, 8),
              builder=..., communicator=...)
  web = hyp.vm("web", cpu=CPU(1), memory=Memory(512), os_drive=OSDrive(pool1, 8),
               builder=..., communicator=...)
  web.needs(db)        # db is fully ready before web boots
  ```

- **`Plan(name, hyp)` validates and freezes everything** into the plan's
  build graph; later `add_*` / `vm()` calls raise. One executor walks that
  graph in topological waves — `testrange graph plan.py --order` shows them,
  and `testrange why plan.py <node>` explains a single node.

[Thinking in build graphs](thinking-in-build-graphs.md) is the full
conceptual tour, including how ordering edges interact (and don't) with the
build cache.

## Networking

### `Switch` owns the infrastructure, `Network` is a label

A `Switch` is one L2 broadcast domain and the only place L2 topology
decisions live: `cidr`, `uplink`, `mgmt`. The services a sidecar VM serves at
`.1` (`dhcp`, `dns`, `nat`) are bundled into an optional `Sidecar` the Switch
carries, not flags on the Switch itself ([ADR-0013](../adr/0013-switch-sidecar-split.md)).
A `Network` is a logical label (port-group) within a Switch —
`add_switch` flattens a switch's Networks into `hyp.networks`, NICs attach
through the handle (`NetworkIface(hyp.networks["netA"], ...)`), and the
orchestrator resolves which Switch owns it.
All Networks on one Switch share `switch.cidr` (multiple Networks =
organizational labels on one wire).

```python
Switch(
    "sw1",
    Network("netA"),
    Network("netB"),          # both on the same wire, same CIDR
    cidr="10.0.1.0/24",       # strict network form; host-form raises
    uplink="egress",          # logical uplink name; the profile maps it to a host iface
    mgmt=True,                # host reachable at .2 on this segment
    sidecar=Sidecar(          # services at .1; omit for a bare L2 wire
        dhcp=True,            # sidecar serves DHCP at .1
        dns=True,             # sidecar serves DNS at .1
        nat=True,             # sidecar MASQUERADEs out the uplink
    ),
)
```

For a per-flag breakdown and the per-driver implementation, see
[Networking modes](drivers/networking-modes.md).

### NIC addressing: static, DHCP, or unconfigured

A NIC's run-phase address mode is set with `addr=`, which takes one of
three values:

```python
NetworkIface(hyp.networks["netA"], addr=StaticAddr("172.31.0.150"))  # static
NetworkIface(hyp.networks["netA"], addr=DHCPAddr())                  # DHCP lease
NetworkIface(hyp.networks["netA"])                                   # addr=None: unconfigured
```

The default is `addr=None` — **unconfigured**, *not* DHCP. The guest's
netplan renders `dhcp4: false` and the OS decides what to do (link-local,
its own client, or nothing). Use `DHCPAddr()` to request a lease (the
Switch needs a `Sidecar(dhcp=True)` for anything to answer) and `StaticAddr(...)`
to pin an address.

Plan-time validation runs when `Plan(...)` freezes the topology and reports
every problem at once. For a `StaticAddr`:

- the address must be inside the owning Switch's CIDR.
- it cannot equal the subnet's network or broadcast address.
- it cannot collide with the pinned sidecar slot (`.1`, present iff the
  Switch has a `sidecar`) or the mgmt slot (`.2`, present iff `mgmt`).
- it cannot fall inside the DHCP pool (`.10`–`.99`) when the sidecar serves
  `dhcp`. Pick something in `.100`–`.254`.
- duplicate static addresses within the same Network across VMs are rejected.

A NIC with `addr=None` or `addr=DHCPAddr()` is left for plan-level
validation to skip — there is no static address to range-check.

### `build_switch` and the build phase

The build phase needs internet access so `apt` / `pip` can pull
packages into the VM's disks. You declare the build network on the
hypervisor via `build_switch` ([ADR-0016](../adr/0016-named-uplinks-out-of-band-egress.md)) —
there is **no default uplink**, so without a `build_switch` the build network
is isolated (no egress):

```python
hyp = Hypervisor(
    build_switch=Switch(
        "build", Network("build"), cidr="10.97.99.0/24", uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
```

`build_switch` is an ordinary `Switch`, realized **identically to a run-phase
switch** — there is no special "managed egress" type. The orchestrator brings
it up (here: a sidecar serving DHCP/DNS and MASQUERADE out the `egress` uplink),
runs each build VM against it, captures every built disk into the cache, and
tears the whole build topology down LIFO before the run phase. Omit
`build_switch` only if every VM already has a cache hit.

`uplink="egress"` is a logical name the bound profile's `[uplinks]` map resolves
to a host bridge with out-of-band internet (NAT/DHCP behind it). TestRange only
attaches to that bridge — it does not manufacture, SNAT, or fence egress; that
is the operator's out-of-band setup. The build switch is portable topology
(it carries no host-specific name), so the same plan runs on any backend whose
profile maps `egress`.

### Data disks (`HardDrive`)

A `HardDrive` is a data disk: zero or more per VM, alongside the single
`OSDrive`. Data disks are *build artifacts* — the build VM boots with every
data disk attached (blank and sized), the cloud-init payload formats and
populates them, and testrange captures each one into the cache. At run the
populated disk is pushed back, so the VM comes up with its data already in
place.

```python
hyp.vm(
    "fileserver",
    cpu=CPU(2),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    data_disks=[HardDrive(pool1, 16)],   # /dev/vdb on the guest; built once, served at run
    nics=[NetworkIface(netA, StaticAddr("172.31.0.150"))],
    builder=...,
    communicator=...,
)
```

Seed the disk in `post_install_commands` (format, mount, write, persist via
`/etc/fstab`); see the `fileserver` VM in `tests/plans/generic/build_cache.py`. Because the disk's `size_gb` and the
data-disk count fold into the build cache key, changing either rebuilds the set.

### Static-NIC netplan staging

When any NIC declares a static address (`StaticAddr`), the cloud-init seed stages two
extra files on the built disk:

1. The real run-phase netplan at `/etc/netplan/50-cloud-init.yaml`.
   Cloud-init's `config` stage writes this AFTER its `init` stage renders
   the build-time DHCP netplan, so the cached disk ends up with the real
   netplan in place.
2. `/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg` so
   cloud-init does not re-render the netplan on subsequent boots.

The run-phase VM gets the cached built disk pushed onto its own ref and
attaches to the user's real networks. The OS boots reading the staged netplan
and comes up on the static address.

### Which NIC does the communicator use?

For communicators that reach the VM over the network (the SSH
communicator is the v0 instance of this pattern), the orchestrator
resolves the bind address from the VM's **first *addressed* NIC** — the
first NIC in device order that carries a `StaticAddr` or `DHCPAddr`
(unconfigured `addr=None` NICs are skipped). To pin a specific NIC
regardless of order, pass `SSHCommunicator("user", nic_idx=N)` where `N`
is the NIC's position in the device list.

```python
hyp.vm(
    "multihomed",
    cpu=CPU(2),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    nics=[
        # Communicator binds to this address (first addressed NIC):
        NetworkIface(hyp.networks["mgmt"], StaticAddr("10.0.0.10")),
        # Also up on the guest, but not used by the communicator:
        NetworkIface(hyp.networks["data"], DHCPAddr()),
    ],
    builder=...,
    communicator=...,
)
```

If the bound NIC is a `StaticAddr`, the orchestrator skips DHCP-lease
lookup and binds to that address directly. If it is a `DHCPAddr`, the
orchestrator reads the lease — keyed on the VM's stable MAC — from the
Switch's sidecar dnsmasq lease file over the driver's native guest agent.

The addressed-NIC rule applies only to communicators that reach the VM over
the network (SSH). `NativeCommunicator` rides the hypervisor's native
guest agent — an in-band channel with no IP — so NIC ordering is
irrelevant to it.

## Readiness is the orchestrator's job

By the time your tests receive the `OrchestratorHandle`, each VM has
already passed its builder's readiness check. For `CloudInitBuilder`
that's `cloud-init status --wait` — i.e., all four cloud-init stages
(`local → network → config → final`) have completed, not just the one
that SSH is ordered after. You don't need to add a `cloud-init status
--wait` test to your suite; if cloud-init never reaches done, bring-up
itself raises `BuildNotReadyError` before tests start.

Each builder owns its own readiness logic and timeout: the orchestrator
hands the builder its VM's `execute` callable, and the builder runs
whatever command it needs. `CloudInitBuilder` allows `cloud-init status
--wait` five minutes — a cold boot's `cloud-final` stage genuinely takes
that long.

## Communicators

A VM's `communicator` is how test code talks to it. Two are built in:

- **`SSHCommunicator("user")`** — connects over SSH to the VM's first
  addressed NIC (or the NIC at `nic_idx=`; see above). Needs a `PosixCred` with a matching username on the
  builder. The default for VMs on a reachable network.
- **`NativeCommunicator()`** — rides the hypervisor's native guest agent
  (QEMU Guest Agent on QEMU-based backends; VMware Tools / Hyper-V
  integration on others): no network, no credentials, no IP discovery for
  QGA. Takes no constructor arguments — the VM *is* the agent's identity. For
  agent backends the guest must have the agent installed and running, which
  you declare yourself in the builder:

  ```python
  builder=CloudInitBuilder(
      base=CacheEntry("debian-13"),
      packages=[Apt("qemu-guest-agent")],
      post_install_commands=("systemctl enable --now qemu-guest-agent",),
  ),
  communicator=NativeCommunicator(),
  ```

  Reach for `NativeCommunicator` when a VM has no reachable NIC (air-gapped
  with no management network), when you don't want SSH on the guest at
  all, or when you need an out-of-band path independent of guest
  networking. See `examples/native_agent.py`.

## API recipes

- **Argv-list execute**: `vm.communicator.execute(["systemctl",
  "is-active", "nginx"], timeout=10.0)` returns an
  `ExecResult(exit_code, stdout, stderr, duration)`. No shell, no
  quoting bugs.
- **Read a file from the guest**: `vm.communicator.read_file("/etc/hosts")` → bytes.
- **Write a file to the guest**: `vm.communicator.write_file("/tmp/x", b"data")`.
- **Tests are functions taking the handle**: `def my_test(orch: OrchestratorHandle) -> None: ...`.
  Raise to fail; the runner captures the traceback into
  `TestResult.error`.

## CLI overview

```
testrange cache add <path-or-url> [--name <pretty>]
testrange cache list
testrange cache push <sha-or-name> --cache <url>   # publish to an HTTP cache
testrange cache pull <sha-or-name> --cache <url>   # fetch from an HTTP cache
testrange describe plan.py
testrange graph plan.py [--order|--dot]            # the build graph / execution waves
testrange why plan.py <node>                       # one node: dependencies + dependents
testrange preflight plan.py --profile <name>       # read-only backend checks
testrange build plan.py                            # warm the cache only; no tests
testrange run plan.py [--fail-fast] [--leak-on-failure] [--require-cache] [--resume RUN_ID]
testrange repl plan.py
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]
```

`build` and `run` are two verbs over the same plan: `build` provisions every
VM to completion and captures its disks into the cache (warming a shared HTTP
tier when `--cache` is set), running no tests. `run` brings the range up from
those cached disks and runs tests — auto-building anything not yet cached, or,
with `--require-cache`, failing fast on a miss so build and run stay distinct,
auditable steps. `run --resume <run_id>` continues a dead run from its ledger.
See [build vs run](build-vs-run.md).

## Tips

- During `build`, the cloud-init seed runs provisioning fail-fast, reports an
  explicit `TESTRANGE-RESULT: ok` on the serial console, then powers off — the
  `ok` token, not the power-off, is what tells the orchestrator the build
  succeeded and is safe to cache ([ADR-0012](../adr/0012-serial-build-result.md)).
  The cached disks are what subsequent runs boot from.
- Don't reuse one `SSHCommunicator(...)` instance across multiple
  VMs; each VM constructs its own. The single-use guard fails loud
  if you try.
- Test functions share the brought-up range. State mutations in one
  test bleed to the next. For per-test reversion, take a snapshot at
  the start and restore at the end via `orch.driver` — see
  [Running tests](running-tests.md) for the snapshot recipe.
- For debugging a failing test, `testrange run --leak-on-failure
  plan.py` retains the brought-up range so you can SSH in. Later,
  tear down with `testrange cleanup <run_id>` (the run id is printed
  on exit).
