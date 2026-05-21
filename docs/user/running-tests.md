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

Exit codes: `0` if all tests passed, `1` on failure, `2` on preflight error
or missing plan file.

## CLI surface

```
testrange describe <plan.py>            # passive structure summary, no backend writes
testrange run <plan.py> [flags]         # bring-up, run TESTS, tear down
  --fail-fast                           #   stop on first test failure
  --leak-on-failure                     #   skip teardown if any test fails
testrange repl <plan.py>                # bring-up, drop into a Python REPL, no TESTS
testrange cleanup <run_id>              # tear down a leaked / crashed run
testrange cleanup --all [--dry-run]     # all stale runs at once
testrange cache add <path-or-url>       # cache subcommands:
testrange cache list                    #   add / list / del / rename / forget-name / push / pull
testrange cache del <sha-or-name>
testrange cache rename <old> <new>
testrange cache forget-name <name>
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
: The driver-side handle (`tr_vm_<run_short>_web`). Pass to driver methods
  like `orch.driver.create_snapshot(vm.backend_name, ...)`.

`vm.communicator: Communicator`
: For guest-side I/O.

## What a communicator exposes

Two are built in — `SSHCommunicator` and `QGACommunicator` (QEMU Guest
Agent). Both implement the same four-method surface:

`execute(argv, *, timeout=60.0, cwd=None) -> ExecResult`
: Run a command in the guest. `argv` is a list; no shell, no quoting
  bugs. `ExecResult` carries `exit_code: int`, `stdout: bytes`,
  `stderr: bytes`, `duration: float`, and an `ok: bool` property
  (`exit_code == 0`).

`read_file(path) -> bytes`
: Read a guest-side file (SFTP for SSH; `guest-file-*` for QGA).

`write_file(path, data)`
: Write a guest-side file.

`close()`
: Release the transport. For `SSHCommunicator` the next `execute`
  reconnects — useful after a driver-level reboot.

`SSHCommunicator` additionally exposes `host: str | None` — the bound
IP, set by the orchestrator during bring-up. `QGACommunicator` has no
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
        # libvirt requires the VM to be inactive before reverting a
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
SSH into the VMs at the discovered IPs to investigate (`virsh
domifaddr <vm-backend-name>` if you don't remember the IP). When
done, tear down:

```sh
testrange cleanup <run_id>
```

`testrange cleanup --all` walks every retained run under
`$XDG_STATE_HOME/testrange/runs/` and tears each down. PID-gated:
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
