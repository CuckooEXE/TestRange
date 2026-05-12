# Bug-fixing

The fastest reproduction loop is the smoke test:

```sh
.venv/bin/python -m testrange.cli --verbose --log-level DEBUG run examples/hello_world.py
```

It exercises the full lifecycle against real libvirt in ~20 seconds
on a warm cache (cache miss adds an install pass, ~3–5 minutes).
Most past bugs surfaced here that unit tests couldn't catch — qcow2
chain semantics, libvirt permission boundaries, paramiko version
quirks.

## Reproduction recipe

1. **Run the smoke test.** Captures stdout/stderr including all the
   driver-level log lines (define vm, start vm, teardown ledger).
2. **`--leak-on-failure`** if a test fails. Skips teardown so you
   can SSH in (the bound IP is in the log).
3. **`virsh` against the leaked VM** to investigate from the host
   side: `virsh -c qemu:///system list`, `virsh dumpxml <name>`,
   `virsh domifaddr <name>`, `virsh snapshot-list <name>`.
4. **`testrange cleanup <run_id>`** to tear down when done.
5. **`testrange cleanup --all`** to sweep all retained runs at
   the end of a session.

## Writing a regression test

When you find a bug, the regression test goes in:

- `tests/unit/test_<file>.py` if it's reproducible without libvirt.
  Most past regressions fit here — XML render bugs, state-machine
  bugs, fake-driver-flow bugs.
- `tests/integration/test_libvirt_driver.py` if it requires a live
  libvirt connection. These are gated by `libvirt-python` being
  importable; they skip otherwise.

Pattern: drive the failing path through the fakes (`_FakeConn`,
`_FakePool`, etc.) and assert on the API call sequence + side
effects. Avoid asserting on exact log strings — those are not part
of the contract.

## Gates that must pass

```sh
ruff check .
ruff format --check .
mypy --strict testrange/
pytest -q
# and:
python -m testrange.cli --verbose --log-level DEBUG run examples/hello_world.py
```

All five gates must pass. The smoke run is the one most easily
forgotten; treat it as part of "done."

## Common gotchas

- **Pool dir leftovers.** Disk-only snapshots leave orphan files in
  the pool dir. `LibvirtDriver.destroy_pool` runs `sp.refresh(0)` +
  `sp.listVolumes()` + per-vol delete to catch these — if you add
  a new path that creates untracked pool files, make sure the
  sweep covers them.
- **Communicator reconnect.** After driver-level reboot or restore,
  the existing paramiko client points at a dead connection. Call
  `vm.communicator.close()` to force a reconnect on the next
  `execute`. The retry loop in `_ensure_connected` handles
  sshd-coming-back-up.
- **Stable MAC.** `LibvirtDriver.compose_mac` derives a stable MAC
  from `(plan_name, vm_name, nic_idx)`. If a test changes one of
  those between runs, the MAC changes, and dnsmasq hands a new IP.
- **Cache-key drift.** `config_hash` is byte-stable as long as the
  rendered seed is byte-stable. The deterministic-from-comment
  Ed25519 keypair is what makes that work; the prior gotcha
  (fresh keypair every import → cache miss every run) is in
  `feedback_no_premature_constants.md`-flavored discipline.

## State on disk during a debugging session

- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.json` — the LIFO
  resource list the cleanup walker uses. Inspect with `jq` if
  cleanup is misbehaving.
- `$XDG_STATE_HOME/testrange/runs/<run_id>/state.pid` — owning
  process. `testrange cleanup` refuses to act on a run whose PID
  is still alive.
- `/var/lib/libvirt/images/testrange/<pool>/` — the on-disk pool
  for system-mode libvirt. Files here outside the orchestrator's
  state are leaks worth investigating.
