# Connecting to a backend

A plan describes *what* range to stand up; a **backend** decides *where*. The
two are separate (ADR-0015): a portable plan declares topology only and pins no
backend, and you supply the backend at run time with a connection profile.

## Writing a portable plan

Use the generic `Hypervisor` as your Plan entry. It carries only topology —
networks, pools, and VMs — and selects no driver:

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
  backend: UNBOUND (pass --connect <profile> to run)
```

`run` and `build` on a portable plan **require** `--connect`; without it they
fail with a clear "this plan is backend-agnostic" error.

## Connecting to your backend

A connection profile is a local TOML file. Copy the example and edit it:

```sh
cp examples/connect.toml.example connect.toml
# edit host / password for your backend
testrange run examples/hello_world.py --connect connect.toml
```

```toml
driver = "proxmox"          # scheme: proxmox | libvirt | mock
host = "10.0.0.5"
user = "root@pam"           # optional; a bare "root" takes the @pam realm
password = "Target123!"
port = 8006                 # optional
verify_ssl = false          # optional
node = ""                   # optional; "" auto-detects the single node
backing_storage = "local"   # optional

[build_switch]              # optional: managed build-internet egress
uplink = "vmbr0"            # host interface to SNAT the build network out of
```

Notes:

- **Secrets are inline.** Passwords live in the file as plain strings;
  testrange backends are firewalled lab environments. A real `connect.toml`
  (and any `*.connect.toml`) is gitignored so it never lands in version
  control — only the tracked `connect.toml.example` is committed. There is no
  environment-variable fallback: `--connect` is the only knob, so an invocation
  is fully self-describing.
- **`[build_switch]` is build-time internet egress.** It maps to a
  `ManagedBuildSwitch`: TestRange manufactures and fences an egress segment for
  the build network and SNATs it out the named host interface (ADR-0014).
  Without it the build network is isolated, so a build that needs `apt`/`pip`
  will fail. A bring-your-own plain-`Switch` egress path isn't expressible in a
  profile — declare it by pinning the plan (below).

`testrange describe --connect connect.toml` prints the resolved binding with
the **password masked**:

```text
Plan (Hypervisor)
  backend:
    driver: proxmox (ProxmoxDriver)
    host: 10.0.0.5
    password: ***set***
    build egress: managed (uplink=vmbr0)
```

## Pinning a plan to a backend

When a test genuinely needs a specific backend, use that backend's concrete
`*Hypervisor` directly. It pins the driver and carries the connection inline —
no profile needed:

```python
from testrange import Plan
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import ManagedBuildSwitch

PLAN = Plan(
    "pve-smoke",
    ProxmoxHypervisor(
        host="10.0.0.5",
        password="Target123!",
        build_switch=ManagedBuildSwitch(uplink="vmbr0"),
        networks=[...], pools=[...], vms=[...],
    ),
)
```

`examples/px_hello.py` is a pinned-Proxmox plan.

A pinned plan still accepts a `--connect` profile, but only to **override the
connection** — the profile's `driver` scheme must match the pinned backend. A
mismatch (e.g. a `mock` profile against a `ProxmoxHypervisor`) is a hard error:
a concrete entry pins the driver, and a profile cannot change it.
