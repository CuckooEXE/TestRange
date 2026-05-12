# Adding a builder

A `Builder` produces the install-time payload that bootstraps a fresh
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
    def config_hash(self, spec: VMSpec, recipe: VMRecipe) -> str: ...

    @abstractmethod
    def render_seed(self, spec: VMSpec, recipe: VMRecipe) -> bytes: ...
```

## The install-cache contract

The cache key for the post-install disk is `config_hash`. Two
constraints:

1. **Deterministic.** Same inputs → same hash. No clocks, no random,
   no run_id. The orchestrator caches the post-install disk by this
   hash; non-determinism means cache misses on every run.
2. **Captures everything that affects the disk.** Credentials,
   packages, post-install commands, base image SHA. If a user
   changes any of them, the hash should change.

A safe default: hash the rendered seed bytes + the base SHA. See
`CloudInitBuilder.config_hash` for the pattern.

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

       def render_seed(self, spec, recipe) -> bytes:
           # Produce the install-time payload as bytes. The orchestrator
           # writes these into a pool volume the install VM mounts.
           ...

       def config_hash(self, spec, recipe, *, base_sha="") -> str:
           # 16-char hex of (rendered seed + base SHA).
           rendered = self.render_seed(spec, recipe)
           h = hashlib.sha256(rendered + base_sha.encode()).hexdigest()[:16]
           return h
   ```

2. **Self-terminate.** The orchestrator polls VM power state during
   install — your seed should end with whatever triggers a shutdown
   on the target OS so the install VM self-terminates and the
   orchestrator can snapshot the resulting disk.

3. **No Communicator during install.** The Communicator only binds
   at run-phase bring-up, against the cached post-install disk. The
   Builder owns the install lifecycle on its own.

4. **Validate inputs at the constructor.** Users construct builders
   directly in their Plan files; mypy isn't part of plan loading.
   Type/value-check at `__init__` and/or in
   `_validate_init_params` for readability.

## Optional dependencies

Same `_import_<dep>()` pattern (see `_import_pycdlib` in the cloud-init
builder). Add a `[<extra>]` to `pyproject.toml`.

## Tests

`tests/unit/test_cloudinit.py` is the template. Most assertions are
on the rendered output (YAML structure, embedded file contents,
`runcmd` ordering). The `config_hash` determinism + sensitivity to
inputs is its own small test class.
