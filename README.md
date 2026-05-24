# testrange

Declarative Python plans → VM test-ranges → user test functions.

Write a plan that declares networks, storage pools, and VMs against a
hypervisor backend; declare test functions; run them. Use case: CI/CD
against specific OS versions and varied network topologies; authorized
pentest test-ranges.

The driver layer is multi-backend (ADR-0008). `MockDriver` is the in-memory
**reference backend** the examples and tests run against; the **Proxmox driver
is green end-to-end** (single-node PVE 9.x — see `examples/px_hello.py`), and a
libvirt driver is planned (rebuilt against the same ABC).

## Quickstart

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all,dev]'

# Populate the cache with a base disk
testrange cache add \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
    --name debian-13

# Inspect a plan without touching the backend
testrange describe examples/hello_world.py

# Warm the cache (build every VM, run no tests)
testrange build examples/hello_world.py

# Bring up the range, run the tests, tear down (auto-builds on a cache miss)
testrange run examples/hello_world.py
```

The example plans target `MockHypervisor` and are the authoritative shape for
writing your own. `testrange describe` works against them with no backend; the
full bring-up lifecycle is exercised against `MockDriver` by the test suite. A
clean live `run` needs a real backend (Proxmox is green on single-node PVE;
libvirt later), which carries its own connection prereqs — see
`docs/user/drivers/`.

## Plan shape

```python
PLAN = Plan(
    MockHypervisor(
        networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24")],
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
testrange cache push / pull <sha-or-name> --cache <url>
testrange describe <plan.py>
testrange build <plan.py>
testrange run <plan.py> [--fail-fast] [--leak-on-failure] [--require-cache]
testrange repl <plan.py>
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]
```

## Docs

Sphinx + furo. Build locally:

```sh
pip install -e '.[docs]'
make -C docs html
# open docs/_build/html/index.html
```

The doc tree:

- **User guide** (`docs/user/`) — install testrange, install your
  driver of choice, write a plan, run tests.
- **Developer guide** (`docs/dev/`) — architecture, how to extend
  (new drivers/devices/communicators/builders), bug-fixing recipes.
- **ADRs** (`docs/adr/`) — load-bearing decisions.

## Status

Pre-1.0. See `docs/dev/architecture.md` (or the built HTML) for the
component overview and `TODO.md` for in-scope and long-term work.
