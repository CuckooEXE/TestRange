# Adding a builder

A `Builder` produces the build-time payload that bootstraps a fresh
VM — credentials, packages, post-install commands. `CloudInitBuilder`
is the only built-in; future ones include Proxmox answer-file, ESXi
kickstart, Windows unattended.xml.

The contract is in `testrange/builders/base.py`:

```python
class Builder(ABC):
    @property
    @abstractmethod
    def credentials(self) -> tuple[Credential, ...]: ...

    @abstractmethod
    def config_hash(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        base_sha: str = "",
        macs: Sequence[str] = (),
    ) -> str: ...

    @abstractmethod
    def render_seed(
        self,
        spec: VMSpec,
        recipe: VMRecipe,
        *,
        addressing: Mapping[str, NetworkAddressing],
        macs: Sequence[str] = (),
    ) -> bytes: ...

    # Non-abstract — default is a no-op (no readiness check).
    def wait_ready(
        self, spec: VMSpec, recipe: VMRecipe, execute: GuestExec
    ) -> None: ...
```

`addressing` is a `Mapping[network_name, NetworkAddressing]` the
orchestrator brokers in — builders stay hypervisor-agnostic and never
see the hypervisor type. `macs` (one per NIC, in spec order) lets a
builder bake positional NIC config (run-phase netplan match-by-MAC,
etc.) into the payload and key the cache on the stable MACs the
orchestrator will assign at run-phase.

## The build-cache contract

The cache key for a VM's built disk set is `config_hash`. Two
constraints:

1. **Deterministic.** Same inputs → same hash. No clocks, no random,
   no run_id. The orchestrator caches the built disks by this
   hash; non-determinism means cache misses on every run.
2. **Captures everything that affects the disk *set*.** Credentials,
   packages, post-install commands, base image SHA — and, since
   [ADR-0010](../../adr/0010-build-run-split.md) §4, the OS-drive `size_gb`
   and each data disk's declaration (count + `size_gb`, in spec order),
   because they change the artifacts the build emits. If a user changes
   any of them, the hash should change.

A safe default: hash the rendered seed bytes + the base SHA + the disk
sizes. See `CloudInitBuilder.config_hash` for the pattern.

## Steps

1. **Subclass `Builder`.** Implement `credentials`, `config_hash`,
   `render_seed`.

   ```python
   class AnswerFileBuilder(Builder):
       def __init__(self, *, base: CacheEntry, credentials=(), ...) -> None:
           ...validate inputs...

       @property
       def credentials(self) -> tuple[Credential, ...]:
           return self._credentials

       def render_seed(self, spec, recipe, *, addressing, macs=()) -> bytes:
           # Produce the build-time payload as bytes. The orchestrator
           # writes these into a pool volume the build VM mounts.
           ...

       def config_hash(self, spec, recipe, *, addressing, base_sha="", macs=()) -> str:
           # 16-char hex of (rendered seed + base SHA).
           rendered = self.render_seed(spec, recipe, addressing=addressing, macs=macs)
           h = hashlib.sha256(rendered + base_sha.encode()).hexdigest()[:16]
           return h
   ```

2. **Self-terminate.** The orchestrator polls VM power state during
   the build — your seed should end with whatever triggers a shutdown
   on the target OS so the build VM self-terminates and the
   orchestrator can capture the resulting disks.

3. **No Communicator during build.** The Communicator only binds
   at run-phase bring-up, against the cached built disks. The
   Builder owns the build lifecycle on its own.

4. **Validate inputs at the constructor.** Users construct builders
   directly in their Plan files; mypy isn't part of plan loading.
   Type/value-check at `__init__` and/or in
   `_validate_init_params` for readability.

5. **Declare run-phase readiness (optional).** If your build leaves
   work to finish at run-phase boot — cloud-init's stage machine,
   Ignition's finalize — override `wait_ready`. It receives an
   `execute` callable (`GuestExec` from `testrange.guest_io` — the
   shape of `Communicator.execute`, injected by the orchestrator);
   run your readiness command through it, inspect the `ExecResult`,
   and raise `BuildNotReadyError` if the VM never becomes ready. The
   builder never sees a Communicator type — only the callable. The
   default is a no-op — right for builders that produce a fully-baked
   disk. Readiness is the orchestrator's job, never something a plan
   author wires into `TESTS`.

## Optional dependencies

Same `_import_<dep>()` pattern (see `_import_pycdlib` in the cloud-init
builder). Add a `[<extra>]` to `pyproject.toml`.

## Tests

`tests/unit/test_cloudinit.py` is the template. Most assertions are
on the rendered output (YAML structure, embedded file contents,
`runcmd` ordering). The `config_hash` determinism + sensitivity to
inputs is its own small test class.
