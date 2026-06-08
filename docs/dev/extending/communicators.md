# Adding a communicator

A `Communicator` is the test-code-facing transport into a running VM.
`SSHCommunicator` and `NativeCommunicator` (a hypervisor's native in-guest
agent: QGA today, with VMware Tools / Hyper-V integration as drivers implement
them) are the built-ins; future transports include WinRM and serial console.

The contract is in `testrange/communicators/base.py`:

```python
class Communicator(ABC):
    @abstractmethod
    def execute(self, argv, *, timeout=60.0, cwd=None) -> ExecResult: ...
    @abstractmethod
    def read_file(self, path) -> bytes: ...
    @abstractmethod
    def write_file(self, path, data) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
```

Note: **the ABC does NOT define a `bind()` method.** Different
communicators bind with wildly different inputs (an IP + credential for
SSH, driver-supplied callables for QGA, a serial path for console).
Each concrete declares its own per-type `bind(...)` signature; the
orchestrator dispatches by communicator type at run-phase bring-up.

## Steps

1. **Subclass `Communicator`.** Implement the four abstract methods.
   The Plan-time constructor takes whatever the user puts in their
   plan — a username for SSH, nothing at all for QGA (the agent's
   identity is the VM). Validate types at the trust boundary in
   `__init__`:

   ```python
   class WinRMCommunicator(Communicator):
       def __init__(self, username: str) -> None:
           if not isinstance(username, str) or not username:
               raise ValueError("WinRMCommunicator(username) must be non-empty")
           self._username = username
           self._bound = False
           # ... transport-specific state, initially None
   ```

2. **Declare your `bind(...)` signature.** Whatever inputs your
   transport needs. The two built-ins show the range:

   ```python
   # SSHCommunicator — addressing the orchestrator discovers (plus an optional
   # GuestGateway to jump through when the guest isn't directly reachable):
   def bind(self, *, host: str, credential: PosixCred, port: int = 22,
            gateway: GuestGateway | None = None) -> None: ...

   # NativeCommunicator — VM-bound callables the orchestrator pulls off
   # the driver. The communicator never imports a driver type:
   def bind(self, *, execute: GuestExec, read_file: GuestReadFile,
            write_file: GuestWriteFile) -> None: ...
   ```

   Always guard against double-bind with a `_bound` flag raising
   `CommunicatorAlreadyBoundError`, and add an `is_bound: bool`
   property. For network transports, connection should be **lazy** —
   the first `execute` opens the connection with a retry loop (see
   `SSHCommunicator._ensure_connected`). A shim over driver callables
   (`NativeCommunicator`) has nothing to connect — it just delegates.

3. **Wire orchestrator dispatch.** In `bind_communicators`
   (`testrange/orchestrator/run_phase.py`), add a branch to the
   `isinstance` ladder:

   ```python
   elif isinstance(comm, WinRMCommunicator):
       ip = discover_ip(ctx, vm)
       cred = lookup_credential(vm)
       comm.bind(host=ip, credential=cred)
   ```

   The `RunContext` already carries the driver, and the VM's backend name
   (`ctx.driver.compose_resource_name(ctx.run_id, "vm", vm.name)`) and the
   discovered IP are available at bind time. The orchestrator is the only
   broker — the communicator never reaches into the driver itself.

4. **Native-agent communicators: use `testrange.guest_io`.** If your
   transport rides a hypervisor's native in-guest agent rather than the
   network, the driver exposes the agent operations via
   `native_guest_execute` / `native_guest_read_file` /
   `native_guest_write_file` (typed as the `GuestExec` /
   `GuestReadFile` / `GuestWriteFile` Protocols in
   `testrange/guest_io.py`). Your communicator's `bind` takes those
   callables and delegates to them — that's all `NativeCommunicator` is.
   The driver-side wire protocol (the QGA client, the VMware Tools
   guest-ops calls, the PowerShell Direct session) lives in the driver,
   never in the communicator.

5. **Don't reuse a communicator across VMs.** Communicators have a
   single-use guard (the `_bound` flag); reusing one is a programmer
   error. The orchestrator constructs the right one per VM from
   `vm.communicator` on the `VMRecipe`.

6. **Honor the `close()` contract.** `close()` must be idempotent.
   For network transports, test code calls `vm.communicator.close()`
   after a driver-level reboot so the next `execute` triggers a
   reconnect — make sure re-`_ensure_connected` works after a close.

## Optional dependencies

Same `_import_<dep>()` pattern as drivers. If your transport needs an
external library, add a `[<extra>]` to `pyproject.toml` and gate the
import.

## Tests

`tests/unit/test_ssh_communicator.py` is the template for a network
transport: mock the underlying SDK at the import point
(`monkeypatch.setattr("testrange.communicators.ssh._import_paramiko",
lambda: fake)`), then drive the full lifecycle (construct, bind,
execute, close, re-execute). `tests/unit/test_native_communicator.py` is
the template for a shim — bind with fake callables and assert it
delegates.
