<p align="center">
  <img src="docs/_static/testrange-logo-horizontal.png" alt="testrange" width="520">
</p>

# testrange

Python plans → validated build graphs → VM test-ranges → user test functions.

Write a plan that registers networks, storage pools, and VMs on a
`Hypervisor` — every cross-reference a typed handle — and freezes into a
validated dependency graph; declare test functions; run them. Use case: CI/CD
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

# Print the plan's build graph and its execution waves
testrange graph examples/hello_world.py --order

# Bind a backend at run time with a connection profile (see
# examples/connect.toml.example for the shape; --profile reads ./connect.toml).
# Warm the cache (build every VM, run no tests):
testrange build examples/hello_world.py --profile libvirt-local

# Bring up the range, run the tests, tear down (auto-builds on a cache miss):
testrange run examples/hello_world.py --profile libvirt-local
```

The example plans use the backend-agnostic `Hypervisor` topology type and are
the authoritative shape for writing your own; a backend is bound at run time via
`--profile`. `testrange describe` shows a plan's topology with no backend,
`testrange graph` renders its frozen build graph (`--order` for the execution
waves; see [thinking in build graphs](docs/user/thinking-in-build-graphs.md)),
and `testrange preflight` runs the read-only checks against the bound backend. A live
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
hyp = Hypervisor()
hyp.add_pool(StoragePool("pool1", 32))
hyp.add_switch(Switch("sw1", Network("netA"), cidr="10.0.1.0/24", mgmt=True))

hyp.add_vm(
    VMRecipe(
        spec=VMSpec(name="web", devices=[
            CPU(2), Memory(1024),
            OSDrive(hyp.pools["pool1"], 8),
            NetworkIface(hyp.networks["netA"], addr=StaticAddr("10.0.1.150")),
        ]),
        builder=CloudInitBuilder(base=CacheEntry("debian-13"), ...),
        communicator=SSHCommunicator("myuser"),
    )
)

PLAN = Plan("hello-world", hyp)   # validates + freezes the build graph

def my_test(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["systemctl", "is-active", "nginx"])
    assert r.exit_code == 0

TESTS = [my_test]
```

`add_*` registers a node and returns its typed handle; devices (and `.needs()`
ordering edges between VMs) take handles, never strings. Every handle reference
becomes a dependency edge, and one executor walks the frozen graph in
topological waves — see
[thinking in build graphs](docs/user/thinking-in-build-graphs.md).

## CLI

```
testrange cache add <path-or-url> [--name <pretty>] [--description <text>]
testrange cache list / del / rename / forget-name
testrange cache purge --yes                        # delete every local entry
testrange cache push / pull <sha-or-name> --cache <url>
testrange describe <plan.py> [--profile <name>]
testrange graph <plan.py> [--order] [--dot] [--cache --profile <name>]
testrange why <plan.py> <node>                     # one node: dependencies, dependents, its wave
testrange preflight <plan.py> --profile <name>     # read-only checks; print each result + exit non-zero on a blocker
testrange build <plan.py> --profile <name>
testrange run <plan.py> --profile <name> [--fail-fast] [--leak-on-failure] [--require-cache] [--resume RUN_ID]
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

2.0 — the plan surface is the imperative builder → frozen build graph
(ADR-0030), a hard break from the 1.x declarative surface; the public API
follows SemVer. See
`docs/dev/architecture.md` (or the built HTML) for the component overview;
in-flight and long-term work lives in `TODO.md` at the repo root (the repo
tracks both the code and the board, which version together).
