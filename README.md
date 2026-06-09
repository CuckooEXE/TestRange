<p align="center">
  <img src="docs/_static/testrange-logo-horizontal.png" alt="testrange" width="520">
</p>

# testrange

Declarative Python plans → VM test-ranges → user test functions.

Write a plan that declares networks, storage pools, and VMs against a
hypervisor backend; declare test functions; run them. Use case: CI/CD
against specific OS versions and varied network topologies; authorized
pentest test-ranges.

The driver layer is multi-backend (ADR-0008). It ships drivers for **libvirt**,
**Proxmox VE**, and **ESXi**, plus an in-memory `MockDriver` the unit suite
drives through the full lifecycle (it simulates the backend, not a real guest).
Each driver's support level — how far it is validated on real hardware — is
stated on its own page under **Support level** (see `docs/user/drivers/`).

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
`--profile`. `testrange describe` shows a plan's topology with no backend, and
`testrange preflight` runs the read-only checks against the bound backend. A live
`run` needs a real backend — see `docs/user/drivers/` for each driver's setup and
support level. The full bring-up lifecycle is also exercised in-memory against
`MockDriver` by the unit suite.

On an interactive terminal, `run`/`build` render a live full-screen dashboard —
panes for per-VM lifecycle state, test pass/fail, a log tail, and the build
serial console; the Log and Serial panes scroll back with the ↑/↓ and PgUp /
PgDn keys (Tab switches panes, End jumps back to the live tail). Piped or in CI
(no TTY), or with `--no-dashboard`, output degrades to plain `rich`-rendered log
lines. See [docs/user/running-tests.md](docs/user/running-tests.md#the-live-dashboard).

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
testrange preflight <plan.py> --profile <name>     # read-only checks; print each result + exit non-zero on a blocker
testrange build <plan.py> --profile <name>
testrange run <plan.py> --profile <name> [--fail-fast] [--leak-on-failure] [--require-cache]
testrange repl <plan.py> --profile <name>
testrange cleanup --list                           # list runs + status, tear down nothing
testrange cleanup <run_id>
testrange cleanup --all [--dry-run]

# Global flags (before the subcommand):
#   --log-level DEBUG|INFO|WARNING|ERROR   set log verbosity (default INFO)
#   --no-dashboard                         disable the live run/build dashboard; plain logs instead
#   --verbose                              surface the build serial console / test output
#   --cache URL                            shared HTTP cache base URL (e.g. https://cache.local:8443)
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

1.0.0 — the public API is stable and follows SemVer. See
`docs/dev/architecture.md` (or the built HTML) for the component overview;
in-flight and long-term work lives in `TODO.md` at the repo root (the repo
tracks both the code and the board, which version together).
