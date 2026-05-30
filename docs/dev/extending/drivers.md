# Adding a hypervisor driver

A driver wraps a backend SDK (proxmoxer, pyvmomi, WMI, ...) and implements
`testrange.drivers.base.HypervisorDriver`. Reference implementation:
`testrange/drivers/mock.py` (`MockDriver`) — an in-memory backend that exercises
the full contract. The deviation analysis behind this shape is
[ADR-0008](../../adr/0008-driver-abc-multi-backend.md).

## Steps

1. **Create the Plan-time data type.** Drivers expose a frozen `@dataclass`
   that users put at the top of their Plan (e.g.
   `MockHypervisor(networks=..., pools=..., vms=...)`). The orchestrator infers
   the driver from this type's class. Run `validate_hypervisor_plan(...)`
   (`testrange.networks.validate`) from its `__post_init__` for the
   backend-agnostic plan checks (structural refs, duplicates, reserved `__`
   prefix, dnsmasq-safe names, addressing); layer any stricter per-backend name
   rule on top.

2. **Subclass `HypervisorDriver`.** Implement every abstract method:

   - `connect()` / `disconnect()` — connection lifecycle.
   - `preflight(plan, *, cache_manager, build_switch)` — **read-only** checks.
     Must not mutate backend state. Call `preflight.mgmt_unsupported_findings(plan)`
     to reject `mgmt=True`, and `preflight.unknown_uplink_findings(switches,
     self.uplinks)` to reject a `Switch.uplink` logical name your profile's
     `[uplinks]` map doesn't define (pass the run switches + `build_switch`);
     verify each pool's `size_gb` fits its backing store; include `build_switch`
     in subnet-overlap checks.
   - `compose_resource_name(run_id, kind, name)` — deterministic backend-safe
     name.
   - `compose_mac(plan_name, vm_name, nic_idx)` — stable MAC under your OUI.
   - `compose_volume_ref(pool_backend_name, vol_name)` — pure function from
     `(pool, name)` to your opaque locator. Holds only for file/dir-style
     storage where you control filenames (e.g. constrain Proxmox to `dir`/`nfs`).
   - **Switch (L2) — the driver owns it.** `create_switch(switch, backend_name)`
     realizes the full fabric (host bridge / vSwitch / vmbr+SDN / VMSwitch);
     the orchestrator never names a bridge. `switch.uplink` is a logical name —
     resolve it through your `uplinks` map (from the profile) to a host iface
     (ADR-0016); egress is out-of-band, so just attach to that iface. For a
     `uplink+nat` Switch, also provision the uplink-facing segment the sidecar's
     `eth1` rides and return its backend network name (else return `None`).
     `destroy_switch` tears the whole fabric down. Attach port-groups with
     `create_network(network, switch, backend_name, *, switch_backend_name)` /
     `destroy_network`.
   - Pools: `create_pool` / `destroy_pool`. A "pool" is a **named namespace in
     pre-existing backing storage** (a libvirt pool, a datastore subdirectory, a
     host dir/share) — not storage you provision. The backing store is static
     driver config.
   - `volume_suffix(kind)` — file extension per volume kind (`build_disk`,
     `run_disk`, `data_disk`, `base_image`, `build_seed`, `sidecar_disk`,
     `sidecar_config`).
   - Volume ops: `write_to_pool`, `upload_to_pool`, `download_from_pool`,
     `create_blank_volume`, `resize_volume`, `delete_volume`. Every disk
     reaches the backend by **host→pool upload** — there is no pool→pool copy.
     `create_blank_volume(ref, size_gb)` provisions a blank sized volume (data
     disks at build; installer-based OS disks later); `resize_volume(ref,
     size_gb)` grows the image-based OS disk before the build boot. (ADR-0010
     §7 removed `create_disk_from_base`: with no shared base and no overlay,
     there is nothing to clone.)
   - VM CRUD: `create_vm`, `start_vm`, `shutdown_vm`, `destroy_vm`,
     `get_vm_power_state`. (There is no `get_lease_ip`: DHCP leases live in the
     per-Switch sidecar, which the orchestrator reads via the native-guest
     transport below — not through the driver.)
   - Native guest transport (optional): override the `native_guest_execute` /
     `native_guest_read_file` / `native_guest_write_file` accessors for the ops
     your backend supports (each defaults to raising `DriverError`). These back
     `NativeCommunicator` and the sidecar lease reads. (A backend whose guest
     channel needs per-call guest credentials — VMware Tools, Hyper-V
     PowerShell Direct — adds an optional `credential` keyword to these
     accessors when it lands; see ADR-0008.)
   - Snapshots: `create_snapshot`, `list_snapshots`, `delete_snapshot`,
     `restore_snapshot`. Raise `DriverError` for `mem=True` if unsupported.

3. **Register the driver** at the bottom of your module:

   ```python
   from testrange.drivers._registry import register

   register(
       hypervisor_cls=MyHypervisor,
       driver_name=MyDriver.DRIVER_NAME,
       from_hypervisor=MyDriver.from_hypervisor,
       from_uri=MyDriver.from_uri,
   )
   ```

   Then add `from testrange.drivers import myhyp as _myhyp` to
   `testrange/drivers/__init__.py` so registration runs at import time.

4. **Honor the locator-type rules.** `Path` means **orchestrator-host
   filesystem path**; `VolumeRef` means **hypervisor-side opaque locator**. See
   `HypervisorDriver`'s class docstring.

## Driver responsibilities (contracts, not signatures)

- **`backend_name` discoverability.** The orchestrator records its
  deterministic composed name *before* create (crash-safe teardown). If your
  real handle is allocated at create time (Proxmox vmid, Hyper-V GUID), stamp
  the composed name into the VM's name/notes/tags and resolve it on `destroy`
  so teardown needs no external map.
- **Out-of-band transport.** Caching lives on the runner, so `upload_to_pool` /
  `download_from_pool` must move bytes between the runner host and the backend.
  This is SDK-native for some backends (libvirt stream, ESXi `/folder` HTTPS)
  but needs a side channel for others (Proxmox download over SSH, Hyper-V over
  SMB/WinRM) carried in driver config. "API only" is not universally possible.
- **Async → sync.** Block on backend tasks (UPID/Task/Job) to completion before
  returning; the ABC is synchronous.

## Optional dependencies

Gate a non-stdlib SDK via the `_import_<dep>()` pattern: wrap `ImportError` into
a typed `DriverError` with an install hint pointing at your `[<extra>]`, and add
the extra to `pyproject.toml`'s `optional-dependencies`.

## Cleanup discipline

The default `destroy(kind, backend_name, **metadata)` dispatch routes:

- `vm`, `build_vm`, `sidecar_vm` → `destroy_vm`
- `switch`, `build_switch` → `destroy_switch`
- `network`, `build_network` → `destroy_network`
- `pool`, `build_pool` → `destroy_pool`
- `build_disk`, `build_seed`, `run_disk`, `data_disk`, `base_image`, `volume`,
  `sidecar_disk`, `sidecar_config`
  → `delete_volume(compose_volume_ref(pool_backend, backend_name))`

For new resource kinds, override `destroy()` and add the kind.

## Tests

Unit-test against an in-memory model, no live backend — `MockDriver`
(`testrange/drivers/mock.py`) is both the reference implementation and the
substrate the orchestrator/ABC tests run against (see
`tests/unit/test_mock_driver.py` and `tests/unit/test_orchestrator.py`). A real
driver adds integration tests under `tests/integration/`, gated by SDK import
availability.
