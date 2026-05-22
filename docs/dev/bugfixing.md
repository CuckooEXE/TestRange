# Bug-fixing

The full orchestration lifecycle (preflight → build → run → bind → test →
cleanup) is exercised against the in-memory `MockDriver` by the unit suite —
`tests/unit/test_orchestrator.py` is the end-to-end driver, with the
communicators faked so no real network or guest is needed:

```sh
.venv/bin/python -m pytest -q
```

That is the fast reproduction loop. It catches orchestration, render, and
state-machine bugs but not real-hypervisor quirks (qcow2 chain semantics,
permission boundaries, paramiko version issues) — those surface when a real
backend driver lands and gets its own integration suite.

The CLI passive check works on any plan with no backend:

```sh
.venv/bin/python -m testrange.cli describe examples/hello_world.py
```

A live `testrange run examples/<x>.py` is **not** a mock smoke test: the
example communicators do real I/O (`SSHCommunicator` opens a real paramiko
connection; `NativeCommunicator` delegates to the driver's guest agent), and
`MockDriver` does not serve a real guest. The end-to-end live `run` smoke
returns with the first real backend (see [drivers](../user/drivers/index.md)).

## Reproduction recipe

1. **Reproduce in a unit test** against `MockDriver` — the fastest loop, and
   where the regression test will live anyway (see below).
2. **`describe`** the plan to sanity-check topology + cache resolution.
3. When a real backend exists: `run --log-level DEBUG`, then
   **`--leak-on-failure`** to retain the range (bound IP is in the log),
   inspect `state.json` (the LIFO ledger), and `testrange cleanup <run_id>` /
   `cleanup --all` when done. Real-backend host-side debugging
   (`virsh`/`qm`/`govc`) arrives with that driver; the mock has no console.

## Writing a regression test

When you find a bug, the regression test goes in:

- `tests/unit/test_<file>.py` — the home for almost everything, since the
  `MockDriver` makes the full orchestration path testable without a hypervisor.
  Most regressions fit here: render bugs, state-machine bugs, driver-flow bugs.
- `tests/integration/` — reserved for tests that need a live backend
  connection. Gated by the backend SDK being importable; they skip otherwise.
  (Empty today; populated when a real driver lands.)

Pattern: drive the failing path through `MockDriver` and assert on the API call
sequence + side effects. Avoid asserting on exact log strings — those are not
part of the contract.

## Gates that must pass

```sh
ruff check .
ruff format --check .
mypy --strict testrange/
pytest -q
# and the smoke run:
python -m testrange.cli --log-level DEBUG run examples/hello_world.py
```

All five gates must pass. The smoke run is the one most easily forgotten;
treat it as part of "done."

## Common gotchas

- **Communicator reconnect.** After a driver-level reboot or restore, the
  existing paramiko client points at a dead connection. Call
  `vm.communicator.close()` to force a reconnect on the next `execute`. The
  retry loop in `_ensure_connected` handles sshd-coming-back-up.
- **Stable MAC.** `driver.compose_mac(plan_name, vm_name, nic_idx)` derives a
  stable MAC. If a test changes one of those between runs, the MAC changes, and
  dnsmasq hands a new IP.
- **Cache-key drift.** `config_hash` is byte-stable as long as the rendered
  seed is byte-stable. The deterministic-from-comment Ed25519 keypair is what
  makes that work; the prior gotcha (fresh keypair every import → cache miss
  every run) is the discipline in `feedback_no_premature_constants`.
- **Pool residue.** Every writable disk reaches the backend by host→pool upload
  and is captured/torn down per run (ADR-0010 §3); a driver's `destroy_pool`
  must sweep any volumes it left behind. If you add a path that creates
  untracked pool files, make sure the sweep covers them.

## State on disk during a debugging session

- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.json` — the LIFO resource
  list the cleanup walker uses. Inspect with `jq` if cleanup is misbehaving.
- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.pid` — owning process.
  `testrange cleanup` refuses to act on a run whose PID is still alive.
- The backend pool root holds the run's disks. For `MockDriver` that is a temp
  directory (`testrange-mock-*`); for a real backend it is the configured pool
  filesystem. Files there outside the orchestrator's state are leaks worth
  investigating.
