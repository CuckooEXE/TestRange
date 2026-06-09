# testrange

Declarative Python plans → VM test-ranges → user test functions.

Write a plan that declares networks, storage pools, and VMs against a
hypervisor backend; declare test functions; run them. Use case: CI/CD
against specific OS versions and varied network topologies; authorized
pentest test-ranges.

The driver layer is multi-backend (ADR-0008). The **libvirt driver is the
certified reference implementation** — green end-to-end on `qemu:///system` as a
plain `libvirt`-group user (`examples/capabilities.py` +
`tests/integration/test_libvirt.py`). `MockDriver` is the in-memory backend the
unit suite drives through the full lifecycle (it simulates the backend, not a
real guest). The **Proxmox driver is in progress** (single-node PVE 9.x).

## Quickstart

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[all,dev]'

# Populate the cache with a base disk
testrange cache add \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
    --name debian-13

# Inspect a plan's topology without touching a backend
testrange describe examples/hello_world.py

# Bind a backend at run time with a connection profile (see
# examples/connect.toml.example for the shape; --profile reads ./connect.toml).
# Warm the cache (build every VM, run no tests):
testrange build examples/hello_world.py --profile libvirt-local

# Bring up the range, run the tests, tear down (auto-builds on a cache miss):
testrange run examples/hello_world.py --profile libvirt-local
```

The example plans use the backend-agnostic `Hypervisor` topology type and are
the authoritative shape for writing your own; a backend is bound at run time via
`--profile`. `testrange describe` shows a plan's topology with no backend. A live
`run` needs a real backend — libvirt is certified (see `docs/user/drivers/`),
Proxmox is in progress. The full bring-up lifecycle is also exercised in-memory
against `MockDriver` by the unit suite.

On an interactive terminal, `run`/`build` render a live dashboard — panes for
per-VM lifecycle state, test pass/fail, a log tail, and the build serial console.
Piped or in CI (no TTY), or with `--no-dashboard`, output degrades to plain
`rich`-rendered log lines. See [docs/user/running-tests.md](docs/user/running-tests.md#the-live-dashboard).

## Plan shape

```python
PLAN = Plan(
    "hello-world",
    Hypervisor(
        networks=[Switch("sw1", Network("netA"), cidr="10.0.1.0/24", mgmt=True)],
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
testrange cache purge --yes                        # delete every local entry
testrange cache push / pull <sha-or-name> --cache <url>
testrange describe <plan.py> [--profile <name>]
testrange build <plan.py> --profile <name>
testrange run <plan.py> --profile <name> [--fail-fast] [--leak-on-failure] [--require-cache]
testrange repl <plan.py> --profile <name>
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]

# Global flags (before the subcommand):
#   --log-level DEBUG|INFO|WARNING|ERROR   set log verbosity (default INFO)
#   --no-dashboard                         disable the live run/build dashboard; plain logs instead
#   --verbose                              surface the build serial console / test output
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
component overview; in-flight and long-term work lives on the `ktui` TestRange
board (the repo tracks code, the board tracks status).
