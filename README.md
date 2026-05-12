# testrange

Declarative Python plans → VM test-ranges → user test functions.

Write a plan that declares networks, storage pools, and VMs against a
libvirt host; declare test functions; run them. Use case: CI/CD
against specific OS versions and varied network topologies; authorized
pentest test-ranges.

## Quickstart

```sh
# Prereqs: libvirt + KVM + group membership (see docs/user/install.md)
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all,dev]'

# Populate the cache with a base disk
testrange cache add \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
    --name debian-13

# Inspect a plan without touching the backend
testrange describe examples/hello_world.py

# Bring up the range, run the tests, tear down
testrange run examples/hello_world.py
```

## Plan shape

```python
PLAN = Plan(
    LibvirtHypervisor(
        connection="qemu:///system",
        networks=[Switch("sw1", Network("netA", "10.0.1.0/24"))],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(name="web", devices=[...]),
                builder=CloudInitBuilder(base=CacheEntry("debian-13"), ...),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)

def my_test(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["systemctl", "is-active", "nginx"])
    assert r.exit_code == 0

TESTS = [my_test]
```

## CLI

```
testrange cache add <path-or-url> [--name <pretty>] [--description <text>]
testrange cache list / del / rename / forget-name
testrange describe <plan.py>
testrange run <plan.py> [--fail-fast] [--leak-on-failure]
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]
```

## Docs

- `docs/user/install.md` — prerequisites and install.
- `docs/user/writing-a-plan.md` — plan API + examples.
- `docs/Architecture-and-Design.md` — component overview.
- `docs/adr/` — load-bearing decisions.

## Status

Pre-1.0. See `docs/Architecture-and-Design.md` for the component overview
and `TODO.md` for in-scope and long-term work.
