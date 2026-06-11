# Running tests

A plan defines two top-level names:

- `PLAN: Plan` — the topology declaration.
- `TESTS: list[Callable[[OrchestratorHandle], None]]` — the test functions
  to run against the brought-up range.

The CLI imports the plan module, brings the range up, runs the tests, and
tears the range down:

```sh
testrange run path/to/plan.py
```

Exit codes: `0` all tests passed (or `build` warmed the cache); `1` a test
failed or the build failed; `2` bad invocation — missing/invalid plan,
preflight reject, or a `run --require-cache` cache miss; `3` cleanup ran but
some resources would not tear down. See [build vs run](build-vs-run.md#exit-codes)
for the same table.

## CLI surface

```
testrange describe <plan.py>            # passive structure summary, no backend writes
testrange preflight <plan.py>           # read-only backend checks; print each result
testrange run <plan.py> [flags]         # bring-up, run TESTS, tear down
  --fail-fast                           #   stop on first test failure
  --leak-on-failure                     #   skip teardown if any test fails
  --jobs N                              #   cap I/O-phase workers (default 8; 0 or 1 = serial)
  --no-dashboard                        #   disable the live dashboard; emit plain log lines
  --verbose                             #   (global) surface the build serial console / test output
testrange repl <plan.py>                # bring-up, drop into a Python REPL, no TESTS
testrange cleanup --list                # list runs + status, tear down nothing
testrange cleanup <run_id>              # tear down a leaked / crashed run
testrange cleanup --all [--dry-run]     # all stale runs at once
testrange cleanup --forget <run_id>     # drop a run's ledger; backend untouched
                                        #   (for a backend that is permanently gone,
                                        #   e.g. a torn-down nested node)
testrange cache add <path-or-url>       # cache subcommands:
testrange cache list                    #   add / list / del / rename / forget-name / purge / push / pull
testrange cache del <sha-or-name>
testrange cache rename <old> <new>
testrange cache forget-name <name>
testrange cache purge --yes             # delete every local entry (local-only; --dry-run to preview)
testrange cache push <sha-or-name> --cache <url>   # publish to an HTTP cache
testrange cache pull <sha-or-name> --cache <url>   # fetch from an HTTP cache
```

## What a test function looks like

```python
from testrange import OrchestratorHandle

def nginx_is_running(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["systemctl", "is-active", "nginx"])
    assert r.exit_code == 0, f"systemctl said: {r.stdout!r}"
```

Tests are plain functions that take one argument (the handle) and raise to
fail. The runner captures the traceback into `TestResult.error`. By default,
all tests run sequentially and the runner continues on failure; pass
`--fail-fast` to stop on the first failed assertion.

Test *execution* is always sequential — your tests share one range. What
parallelizes is the bring-up plumbing underneath: per-VM disk uploads,
build-disk captures, and readiness waits run on a bounded thread pool so a
multi-VM range comes up in roughly the time of its slowest VM instead of the
sum. `--jobs N` caps that pool (default 8); `--jobs 0` or `--jobs 1` forces it
serial, which is handy when a backend misbehaves under concurrency or you want
deterministic single-threaded logs while debugging.

## The live dashboard

On an interactive terminal, `run` and `build` render a live full-screen dashboard
while the range comes up — four panes that update in place. The top row (VMs +
Tests) takes a fifth of the height; the streaming Log + Serial panes take the
rest:

- **VMs** — each VM and its current lifecycle stage
  (`pending → provisioning → building → booting → binding → ready`, or `failed`
  with the error), colour-coded, with elapsed time.
- **Tests** — each test as it runs, then `✓`/`✗` with its duration.
- **Log** — a tail of the orchestrator's progress log.
- **Serial (build)** — the build VM's serial console as it streams, so a slow or
  stuck install is visible instead of silent.

The Log and Serial panes scroll back through their ring buffers so you can read
output that has already streamed past: **Tab** (or ←/→) switches the focused
pane, **↑/↓** scroll a line, **PgUp/PgDn** a page, **Home** (or `g`) jumps to the
oldest line and **End** (or `G`) snaps back to the live tail.

The dashboard runs on the alternate screen buffer (so it never flickers and
leaves your scrollback untouched), which means its final frame is gone once it
exits; a one-line pass/fail tally and the test report (`[PASS]/[FAIL]` lines)
print on the restored screen afterwards.

It activates only on a real TTY. When output is piped or redirected (CI, a log
file), or when you pass `--no-dashboard`, there is no live region — logging falls
back to plain, greppable lines. `--verbose` additionally surfaces the build
serial console and per-test stdout/stderr (in the Serial pane on a TTY, or as
plain log lines off one); without it those high-volume firehoses stay quiet
regardless of `--log-level`.

## What `OrchestratorHandle` exposes

`orch.run_id: str`
: The current run's id (UTC timestamp + nonce). Useful to log; same id
  appears in `testrange cleanup <run_id>` if you `--leak-on-failure`.

`orch.driver: HypervisorDriver`
: The live hypervisor driver. Use it for host-side VM ops the
  communicator can't do — snapshots, power state, reboot. See the
  snapshot recipe below.

`orch.vms: Mapping[str, VMHandle]`
: Keyed by the Plan name (e.g., `"web"`).

`vm.name: str`
: User-facing Plan name.

`vm.backend_name: str`
: The driver-side handle (`tr-vm-<run_id[:8]>-web`). Pass to driver methods
  like `orch.driver.create_snapshot(vm.backend_name, ...)`.

`vm.communicator: Communicator`
: For guest-side I/O.

## What a communicator exposes

Two are built in — `SSHCommunicator` and `NativeCommunicator` (the
hypervisor's native guest agent). Both implement the same four-method
surface:

`execute(argv, *, timeout=60.0, cwd=None) -> ExecResult`
: Run a command in the guest. `argv` is a list; no shell, no quoting
  bugs. `ExecResult` carries `exit_code: int`, `stdout: bytes`,
  `stderr: bytes`, `duration: float`, and an `ok: bool` property
  (`exit_code == 0`).

`read_file(path) -> bytes`
: Read a guest-side file (SFTP for SSH; the driver's native guest-file
  channel for `NativeCommunicator`).

`write_file(path, data)`
: Write a guest-side file.

`close()`
: Release the transport. For `SSHCommunicator` the next `execute`
  reconnects — useful after a driver-level reboot.

`SSHCommunicator` additionally exposes `host: str | None` — the bound
IP, set by the orchestrator during bring-up. `NativeCommunicator` has no
address; it reaches the VM through the hypervisor's guest-agent
channel. See [Writing a plan](writing-a-plan.md#communicators) for when
to pick which.

## Snapshots / per-test revert

The driver exposes a small snapshot API. The recipe for "take a snapshot,
do destructive work, restore" inside a test:

```python
def reboot_persists_then_revert(orch: OrchestratorHandle) -> None:
    vm = orch.vms["web"]
    driver = orch.driver
    vm_be = vm.backend_name

    driver.create_snapshot(vm_be, "pre-test", "before destructive work")
    try:
        # destructive work, ideally hermetic
        vm.communicator.execute(["touch", "/home/myuser/oops"])
    finally:
        # A backend may require the VM to be inactive before reverting a
        # disk-only snapshot. shutdown_vm() waits for power-off.
        driver.shutdown_vm(vm_be, timeout=60.0)
        driver.restore_snapshot(vm_be, "pre-test")
        driver.start_vm(vm_be)
        vm.communicator.close()  # drop the stale paramiko client
        driver.delete_snapshot(vm_be, "pre-test")
```

The driver also exposes `list_snapshots(vm_be) -> list[str]`. Snapshots
left behind at the end of a test are reaped by the orchestrator's
teardown.

`examples/hello_world.py::snapshot_lifecycle` is the worked example —
it snapshots, writes a sentinel file, reboots the VM (verifies the
file persists), restores the snapshot (verifies the file is gone),
all in ~20s.

## Live debugging on failure

```sh
testrange run --leak-on-failure plan.py
```

If any test fails, teardown is skipped. The CLI prints the `run_id`.
SSH into the VMs at the discovered IPs to investigate — the bound IP is
logged during bring-up, and for DHCP NICs it is the sidecar lease the
orchestrator read over the native guest agent. When done, tear down:

```sh
testrange cleanup <run_id>
```

Not sure which runs are still around? `testrange cleanup --list` enumerates
every retained run with its status (`running` if a live process still owns it,
`stopped` otherwise), phase, plan name, resource count, and creation time —
and tears nothing down:

```sh
testrange cleanup --list
```

```
RUN ID                    STATUS    PHASE       PLAN                RES  CREATED
20260608-141102-9af3c1    running   run         hello                 6  2026-06-08T14:11:02Z
20260608-093344-1b7e02    stopped   run         nested-esxi           9  2026-06-08T09:33:44Z
```

(`PLAN` is the plan's name; the source `.py` file that spawned the run is not
recorded in state.)

`testrange cleanup --all` walks every retained run under
`$XDG_STATE_HOME/testrange/runs/` and tears each down. Lock-gated:
refuses to act on a run whose owning process is still alive.

## What `examples/hello_world.py` shows

The shipped example exercises every load-bearing path:

- A two-network plan with `nginx` installed via cloud-init.
- `nginx_is_installed` — proves the apt package install ran.
- `hostname_matches` — proves the cloud-init meta-data hostname stuck.
- `snapshot_lifecycle` — the snapshot recipe above end-to-end.

There is no `cloud_init_finished` test: builder readiness is the
orchestrator's job, not the plan author's. By the time `TESTS` run,
each VM has already passed its builder's readiness check (for
`CloudInitBuilder`, `cloud-init status --wait`) — see
[Writing a plan](writing-a-plan.md#readiness-is-the-orchestrators-job).

It's a good starting template — copy it and adapt the `PLAN` /
`TESTS` to your topology.
