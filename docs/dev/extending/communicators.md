# Adding a communicator

A `Communicator` is the test-code-facing transport into a running VM.
`SSHCommunicator` is the only built-in; future ones include QGA
(QEMU Guest Agent), VMware Tools, WinRM, and serial console.

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
communicators bind with wildly different inputs (an IP for SSH, a
libvirt connection + domain name for QGA, a serial path for console).
Each concrete declares its own per-type `bind(...)` signature; the
orchestrator dispatches by communicator type at run-phase bring-up.

## Steps

1. **Subclass `Communicator`.** Implement the four abstract methods.
   The Plan-time constructor takes whatever the user puts in their
   plan (typically a username or a service-account identifier).
   Validate types at the trust boundary in `__init__`:

   ```python
   class QGACommunicator(Communicator):
       def __init__(self, username: str) -> None:
           if not isinstance(username, str) or not username:
               raise ValueError("QGACommunicator(username) must be non-empty")
           self._username = username
           self._bound = False
           # ... transport-specific state, initially None
   ```

2. **Declare your `bind(...)` signature.** Whatever inputs your
   transport needs:

   ```python
   def bind(self, *, libvirt_conn: Any, vm_backend_name: str) -> None:
       if self._bound:
           raise CommunicatorAlreadyBoundError(...)
       # ... store inputs, lazy-connect on first execute
   ```

   Add a `is_bound: bool` property and a `_ensure_connected()`
   helper. Connection should be **lazy** — first `execute` call
   opens the connection with a retry loop (the VM may take time to
   come up).

3. **Wire orchestrator dispatch.** In `Orchestrator._bind_communicators`
   (`testrange/orchestrator/runtime.py`), add the per-type branch:

   ```python
   elif isinstance(vm.communicator, QGACommunicator):
       vm.communicator.bind(
           libvirt_conn=self.driver.conn,
           vm_backend_name=...,
       )
   ```

   The orchestrator already has the driver + the VM's backend name
   available at bind time.

4. **Don't reuse a communicator across VMs.** Communicators have a
   single-use guard (the `_bound` flag); reusing one is a
   programmer error. The orchestrator constructs the right one per
   VM from `vm.communicator` on the `VMRecipe`.

5. **Honor the `close()` contract.** `close()` must be idempotent.
   Test code calls `vm.communicator.close()` after a driver-level
   reboot so the next `execute` triggers a reconnect. Make sure
   re-`_ensure_connected` works after a close.

## Optional dependencies

Same `_import_<dep>()` pattern as drivers. If your transport needs an
external library, add a `[<extra>]` to `pyproject.toml` and gate the
import.

## Tests

`tests/unit/test_ssh_communicator.py` is the template. Mock the
underlying SDK at the import point (`monkeypatch.setattr(
"testrange.communicators.ssh._import_paramiko", lambda: fake)`),
then drive the communicator through its full lifecycle (construct,
bind, execute, close, re-execute).
