# Writing a Plan

A `testrange` plan is a Python file that declares a top-level
``PLAN = Plan(...)`` and a ``TESTS = [...]`` list. The CLI imports
the file and uses both.

## Minimal plan

```python
from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred, SSHKey
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

# Deterministic from `comment`; same comment -> same keypair across runs,
# which keeps the rendered cloud-init seed byte-stable so the post-install
# cache hits on subsequent invocations. Insecure by design; test-only.
_KEY = SSHKey.generate(comment="hello")

PLAN = Plan(
    LibvirtHypervisor(
        connection="qemu:///system",
        install_uplink="eth0",
        networks=[
            Switch(
                "sw1",
                Network("netA"),
                cidr="10.0.1.0/24",
                uplink="eth0",
                dhcp=True,
                dns=True,
                nat=True,
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
                        LibvirtNetworkIface("netA"),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred(
                            "alice",
                            pubkey=_KEY.auth_line,
                            privkey=_KEY.priv,
                            sudo=True,
                        ),
                    ],
                    packages=[Apt("nginx")],
                ),
                communicator=SSHCommunicator("alice"),
            ),
        ],
    ),
    name="hello",
)

def nginx_is_running(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["systemctl", "is-active", "nginx"])
    assert r.exit_code == 0, r

TESTS = [nginx_is_running]
```

Then:

```sh
testrange cache add https://cloud.debian.org/.../debian-13-generic-amd64.qcow2 \
    --name debian-13
testrange describe path/to/plan.py
testrange run path/to/plan.py
```

## Networking

### `Switch` owns the infrastructure, `Network` is a label

A `Switch` is one L2 broadcast domain and the only place network
infrastructure decisions live: `cidr`, `uplink`, `mgmt`, `dhcp`, `dns`,
`nat`. A `Network` is a logical label (port-group) within a Switch —
VMs attach by name, and the orchestrator resolves which Switch owns it.
All Networks on one Switch share `switch.cidr` (multiple Networks =
organizational labels on one wire).

```python
Switch(
    "sw1",
    Network("netA"),
    Network("netB"),          # both on the same wire, same CIDR
    cidr="10.0.1.0/24",       # strict network form; host-form raises
    uplink="eth0",            # physical NIC on the host (vSwitch model)
    mgmt=True,                # host reachable at .2 on this segment
    dhcp=True,                # sidecar serves DHCP at .1
    dns=True,                 # sidecar serves DNS at .1
    nat=True,                 # sidecar MASQUERADEs out the uplink
)
```

For a per-flag breakdown and the per-driver implementation, see
[Networking modes](drivers/networking-modes.md).

### Static IPs vs DHCP

Each NIC is DHCP by default. Pin a static address with `ipv4=`:

```python
LibvirtNetworkIface("netA", ipv4="172.31.0.150")  # static
LibvirtNetworkIface("netA")                       # DHCP
```

Plan-time validation runs at Hypervisor construction and reports every
problem at once:

- `ipv4` must be inside the owning Switch's CIDR.
- `ipv4` cannot equal the subnet's network or broadcast address.
- `ipv4` cannot collide with the pinned sidecar slot (`.1`, present iff
  `dhcp|dns|nat`) or the mgmt slot (`.2`, present iff `mgmt`).
- `ipv4` cannot fall inside the DHCP pool (`.10`–`.99`) when `dhcp=True`.
  Pick something in `.100`–`.254`.
- A NIC without `ipv4` attached to a Switch with `dhcp=False` is rejected
  (the NIC would never get an address at run-phase).
- Duplicate static IPs within the same Network across VMs are rejected.

### `install_uplink` and the install phase

The install phase needs internet access so `apt` / `pip` can pull
packages into the VM's post-install disk. Declare the physical NIC on
the libvirtd host that gives that egress:

```python
LibvirtHypervisor(
    connection="qemu:///system",
    install_uplink="eth0",
    ...
)
```

The orchestrator synthesizes a transient install Switch
(`10.97.99.0/24`, dhcp + dns + nat) using that uplink, brings up a
sidecar to serve DHCP and MASQUERADE outbound, runs each install VM
against it, snapshots the post-install disk into the cache, and tears
the whole install topology down LIFO before the run phase. Skip
`install_uplink=` only if every VM already has a cache hit.

### Static-NIC netplan staging

When any NIC declares a static `ipv4`, the cloud-init seed stages two
extra files on the post-install disk:

1. The real run-phase netplan at `/etc/netplan/50-cloud-init.yaml`.
   Cloud-init's `config` stage writes this AFTER its `init` stage renders
   the install-time DHCP netplan, so the cached disk ends up with the real
   netplan in place.
2. `/etc/cloud/cloud.cfg.d/99-testrange-disable-network.cfg` so
   cloud-init does not re-render the netplan on subsequent boots.

The run-phase VM clones the cached disk and attaches to the user's real
networks. The OS boots reading the staged netplan and comes up on the
static address.

### Which NIC does the communicator use?

For communicators that reach the VM over the network (the SSH
communicator is the v0 instance of this pattern), the orchestrator
resolves the bind address from the **first** declared NIC of the VM.
If you declare multiple NICs and want the communicator to reach the
VM via a specific one, declare that NIC first. The other NICs are
brought up on the guest (per the staged netplan) but are not used for
the communicator's connection.

```python
VMSpec(
    name="multihomed",
    devices=[
        CPU(2), Memory(1024), OSDrive("pool1", 8),
        # Communicator binds to this address:
        LibvirtNetworkIface("mgmt", ipv4="10.0.0.10"),
        # Available on the guest, not used by the communicator:
        LibvirtNetworkIface("data"),
    ],
)
```

If the first NIC is static, the orchestrator skips DHCP-lease lookup and
binds to `nic.ipv4` directly. If the first NIC is DHCP, the orchestrator
polls the driver for the lease keyed on the stable MAC.

The first-NIC rule applies only to communicators that reach the VM over
the network (SSH). `QGACommunicator` rides the hypervisor's native
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
  NIC (see above). Needs a `PosixCred` with a matching username on the
  builder. The default for VMs on a reachable network.
- **`QGACommunicator()`** — rides the hypervisor's native guest agent
  (QEMU Guest Agent on libvirt): no network, no credentials, no IP
  discovery. Takes no constructor arguments — the VM *is* the agent's
  identity. The guest must have `qemu-guest-agent` installed and
  running, which you declare yourself in the builder:

  ```python
  builder=CloudInitBuilder(
      base=CacheEntry("debian-13"),
      packages=[Apt("qemu-guest-agent")],
      post_install_commands=("systemctl enable --now qemu-guest-agent",),
  ),
  communicator=QGACommunicator(),
  ```

  Reach for `QGACommunicator` when a VM has no reachable NIC (air-gapped
  with no management network), when you don't want SSH on the guest at
  all, or when you need an out-of-band path independent of guest
  networking. See `examples/qga.py`.

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
testrange describe plan.py
testrange run plan.py [--fail-fast] [--leak-on-failure]
testrange repl plan.py
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]
```

## Tips

- The cloud-init seed `runcmd` always ends with `poweroff` so the
  install VM self-terminates. The cached disk is what subsequent
  runs boot from.
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
