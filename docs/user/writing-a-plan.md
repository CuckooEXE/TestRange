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
from testrange.credentials import PosixCred, gen_ssh_key
from testrange.devices import CPU, LibvirtNetworkIface, Memory, OSDrive, StoragePool
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

_KEY = gen_ssh_key()

PLAN = Plan(
    LibvirtHypervisor(
        connection="qemu:///session",
        networks=[Switch("sw1", Network("netA", "10.0.1.0/24"))],
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
                            pubkey=_KEY.public,
                            privkey=_KEY.private,
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
  test bleed to the next (PLAN.md decision 14). Per-test snapshots
  are a long-term TODO.
- For debugging a failing test, `testrange run --leak-on-failure
  plan.py` retains the brought-up range so you can SSH in. Later,
  tear down with `testrange cleanup <run_id>` (the run id is printed
  on exit).
