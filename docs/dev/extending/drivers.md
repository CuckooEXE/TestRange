# Adding a hypervisor driver

A driver wraps a backend SDK (libvirt-python, proxmoxer, pyvmomi, ...)
and implements `testrange.drivers.base.HypervisorDriver`. Reference
implementation: `testrange/drivers/libvirt.py`.

## Steps

1. **Create the Plan-time data type.** Drivers expose a frozen
   `@dataclass` that users put at the top of their Plan
   (`LibvirtHypervisor(connection=..., networks=..., pools=...,
   vms=...)`). The orchestrator infers the driver from this type's
   class.

2. **Subclass `HypervisorDriver`.** Implement every abstract method.
   The grouping:

   - `connect()` / `disconnect()` — connection lifecycle.
   - `preflight(plan, *, cache_manager)` — **read-only** checks
     (subnet overlap, cache resolution, etc.). Must not mutate
     backend state.
   - `compose_resource_name(run_id, kind, name)` — deterministic
     backend-safe name.
   - `compose_mac(plan_name, vm_name, nic_idx)` — stable MAC under
     the right OUI for your backend.
   - `compose_volume_ref(pool_backend_name, vol_name)` — pure
     function from `(pool, name)` to the opaque locator your driver
     uses.
   - `volume_suffix(kind)` — file extension for a given volume kind
     (`install_disk`, `run_disk`, `base_image`, `install_seed`).
   - Network/pool CRUD: `create_network`, `destroy_network`,
     `create_pool`, `destroy_pool`.
   - Volume ops: `write_to_pool(target_ref, data)`,
     `upload_to_pool(target_ref, source_path)`,
     `create_disk_from_base(target_ref, source_ref)`,
     `download_from_pool(vol_ref, dest_path)`,
     `delete_volume(vol_ref)`.
   - VM CRUD: `create_vm`, `start_vm`, `shutdown_vm`,
     `destroy_vm`, `get_vm_power_state`, `get_lease_ip`.
   - Snapshots: `create_snapshot`, `list_snapshots`,
     `delete_snapshot`, `restore_snapshot`. Drivers that don't
     support memory snapshots raise `DriverError` when `mem=True`.

3. **Register the driver.** At the bottom of your driver module, call
   `testrange.drivers._registry.register(...)`:

   ```python
   from testrange.drivers._registry import register

   register(
       hypervisor_cls=MyHypervisor,
       driver_name=MyDriver.DRIVER_NAME,
       from_hypervisor=lambda hyp: MyDriver(uri=hyp.connection),
       from_uri=lambda uri: MyDriver(uri=uri),
   )
   ```

   Then add `from testrange.drivers import myhyp as _myhyp` to
   `testrange/drivers/__init__.py` so the registration runs at
   import time.

4. **Honor the locator-type rules.** `Path` parameters/returns on
   the ABC mean **orchestrator-host filesystem path**. `VolumeRef`
   means **hypervisor-side opaque locator** — what your backend
   uses internally. See `HypervisorDriver`'s class docstring for
   the precise rule.

## Optional dependencies

If your driver depends on a non-stdlib SDK, gate it via the
`_import_<dep>()` pattern (see `_import_libvirt` in the libvirt
driver). Wrap the `ImportError` into a typed `DriverError` with an
install hint pointing at your `[<extra>]`. Add the extra to
`pyproject.toml`'s `optional-dependencies`.

## Cleanup discipline

The default `destroy(kind, backend_name, **metadata)` dispatch on
`HypervisorDriver` routes:

- `vm`, `install_vm` → `destroy_vm`
- `network`, `install_network` → `destroy_network`
- `pool` → `destroy_pool`
- `install_disk`, `install_seed`, `run_disk`, `base_image`, `volume`
  → `delete_volume(compose_volume_ref(pool_backend, backend_name))`

If your backend introduces new resource kinds, override `destroy()` on
your driver and add the kind there. Otherwise the inherited dispatch
covers the standard set.

## Tests

Unit-test against fakes (no live backend). The libvirt driver's
`tests/unit/test_libvirt_driver_unit.py` is the template — a
`_FakeConn` / `_FakePool` / `_FakeStorageVol` mock the libvirt SDK
shape; tests assert on XML strings + call sequences. Integration
tests live under `tests/integration/` and are gated by import
availability (skip if your SDK isn't installed).
