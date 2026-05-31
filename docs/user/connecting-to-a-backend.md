# Connecting to a backend

A plan describes *what* range to stand up; a **backend** decides *where*. The
two are separate (ADR-0015): a plan declares topology only — and optionally
constrains *which* backend scheme is allowed — while the connection always
arrives at run time via a connection profile.

## Writing a portable plan

Use the generic `Hypervisor` as your Plan entry. It carries only topology —
networks, pools, and VMs — and pins no backend:

```python
from testrange import Hypervisor, Plan
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec
# ... builder / communicator imports ...

PLAN = Plan(
    "hello-world",
    Hypervisor(
        networks=[
            Switch("switch1", Network("netA"), cidr="172.31.0.0/24",
                   sidecar=Sidecar(dhcp=True, dns=True)),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[...],
    ),
)
```

A portable plan carries no host address and no password — those move into the
profile. `examples/hello_world.py` is a complete portable plan.

`testrange describe` renders a portable plan and reports the backend as
`UNBOUND` until you pass a profile:

```text
Plan (Hypervisor)
  backend: UNBOUND (pass --profile <name> to run)
```

`run` and `build` on a portable plan **require** `--profile`; without it they
fail with a clear "this plan is backend-agnostic" error.

## Connecting to your backend

A connection profile is one named table in a local TOML file. Copy the example
and edit it, then select a profile by name:

```sh
cp examples/connect.toml.example connect.toml
# edit host / password + the [<profile>.uplinks] map for your backend
testrange run examples/hello_world.py --profile pve
```

`--profile pve` reads the `[pve]` table from `./connect.toml`; use
`--profile other.toml:pve` to read it from a different file. One file holds many
profiles (ADR-0016):

```toml
[pve]
driver = "proxmox"          # scheme: proxmox | libvirt | mock
host = "10.0.0.5"
user = "root@pam"           # optional; a bare "root" takes the @pam realm
password = "Target123!"
port = 8006                 # optional
verify_ssl = false          # optional
node = ""                   # optional; "" auto-detects the single node
backing_storage = "local"   # optional

[pve.uplinks]               # logical-name -> host iface (ADR-0016)
egress = "vmbr0"            # the out-of-band bridge a NAT sidecar routes out of
```

Each backend ships its own concrete `BackendProfile` subclass (CORE-18) — the
`ProxmoxProfile` shown above; `LibvirtProfile(uri, uplinks)` (no `backing_pool`
— libvirt creates per-run dir pools itself);
`MockProfile(pool_root, backing_capacity_gb)` for the in-memory backend. The
`driver = "..."` scheme picks which one TestRange loads, and each subclass
rejects keys it doesn't know (typo protection).

Notes:

- **Secrets are inline.** Passwords live in the file as plain strings;
  testrange backends are firewalled lab environments. A real `connect.toml`
  (and any `*.connect.toml`) is gitignored so it never lands in version
  control — only the tracked `connect.toml.example` is committed. There is no
  environment-variable fallback: `--profile` is the only knob, so an invocation
  is fully self-describing.
- **`[uplinks]` maps logical uplink names to host ifaces.** A plan's
  `Switch(uplink="egress")` resolves through this map to a host bridge
  (ADR-0016); an unmapped name fails at preflight. Egress is **out-of-band**: a
  named uplink is a host bridge you provision (NAT/DHCP behind it) — TestRange
  only attaches to it, it never manufactures, SNATs, or fences it. Because the
  uplink is a logical name, the plan stays host-free; build-time internet egress
  is the plan's `build_switch` (see "writing a plan"), not a profile knob.

`testrange describe --profile pve` prints the resolved binding with the
**password masked**:

```text
Plan (Hypervisor)
  backend:
    driver: proxmox (ProxmoxDriver)
    host: 10.0.0.5
    password: ***set***
    uplinks: egress→vmbr0
    build egress: switch 'build' (uplink=egress)
```

## Constraining a plan to a backend scheme

When a test genuinely depends on a specific backend (a PVE-specific CPU type
or SDN feature, a libvirt-specific NIC model), use that backend's concrete
`*Hypervisor` as the Plan entry. Under CORE-19 it is a **topology-only scheme
marker** — same shape as the generic `Hypervisor`, but it asserts *this
topology MUST run against backend X*:

```python
from testrange import Plan
from testrange.drivers.proxmox import ProxmoxHypervisor

PLAN = Plan(
    "pve-smoke",
    ProxmoxHypervisor(
        networks=[...], pools=[...], vms=[...],
    ),
)
```

`examples/px_hello.py` is a scheme-pinned-Proxmox plan.

A scheme-pinned plan still **requires** `--profile` — the entry carries no
connection. The profile's `driver` scheme **must** match the pinned scheme; a
mismatch (e.g. a `mock` profile against a `ProxmoxHypervisor`) is a hard
error. Running `testrange describe` on a pinned plan with no `--profile`
renders an UNBOUND binding that names the required scheme:

```text
Plan (ProxmoxHypervisor)
  backend: UNBOUND (pinned to 'proxmox'; pass --profile <proxmox-profile>)
```
