# Extending testrange

`testrange` is built around a small set of abstract bases that
concretes plug into. Adding a new hypervisor backend, a new device
shape, a new transport for guest I/O, or a new install-payload format
is meant to be a one-file change in most cases.

```{toctree}
:maxdepth: 1

drivers
devices
communicators
builders
```

## The stovepipe rule

Before adding anything, internalize the stovepipe rule:

- **Builders**, **Communicators**, and **Credentials** never know about
  each other.
- The **Orchestrator** is the only thing that brokers between them —
  it pulls `builder.credentials` and hands the right one to the
  Communicator at bind time.
- **Drivers** know nothing about Plan-time data types except through
  the small data classes they accept (`VMSpec`, `StoragePool`,
  `Network`, `Switch`).

When two modules would otherwise need to know about each other, the
orchestrator brokers. Concretes don't import sideways.

## The intent-then-confirm protocol

Every backend resource the orchestrator creates is recorded in
`state.json` **before** the backend create-call:

```python
self._store.record_intent(kind="run_disk", backend_name=name, plan_name=vm.name,
                          pool_backend=pool_backend)
self.driver.upload_to_pool(target_ref, built_disk_path)
self._store.confirm(name, pool_backend=pool_backend)
```

If the process dies between `record_intent` and `confirm`, cleanup
still finds the deterministic backend name and tries to destroy it.
When adding a new resource kind that needs cleanup, follow this
sandwich; populate `**metadata` at intent time so the destroy
dispatcher has what it needs even if confirm never ran.
