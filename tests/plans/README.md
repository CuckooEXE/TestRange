# `tests/plans/` — the backend certification & regression corpus

These are **TestRange Plans**, not pytest tests. Each file is a standalone
`PLAN` + a few `TESTS` functions, run with `testrange run` against a bound
backend. They are the canonical way a hypervisor driver gets **certified green**
and the way that certification is held against **regressions**.

> **Not collected by pytest.** Files here are named without a `test_` prefix and
> the tree has no `__init__.py`, so `pytest` (which collects `test_*.py`) never
> picks them up. They are linted and type-checked (`ruff`, `mypy --strict`) like
> the rest of `tests/`, but they only *execute* via `testrange run`. This keeps
> the unit gate (`pytest -m "not proxmox and not libvirt"`) honest — bringing up
> a real VM range is not a unit test.

## How a new backend gets certified

1. **Run every `generic/` plan** against the new backend. They are portable
   (plain `Hypervisor`, logical `uplink="egress"`, no host/creds), so a green
   sweep proves the driver honours the portable contract:

   ```sh
   for p in tests/plans/generic/*.py; do
       testrange run --profile <name> "$p" || break
   done
   ```

2. **Author the backend's own `<driver>/` plans** for surface only that backend
   exposes (controller bus/model, firmware, datastore/vmdk specifics …), using
   that backend's **concrete device types** (e.g. `LibvirtDataDrive(bus=...)`,
   `ESXiHardDrive(bus=...)`). Run them with the matching profile.

A backend is **certified** when its generic sweep *and* its `<driver>/` sweep are
green. Re-running the corpus after any change is the regression guard.

## Layout

```
tests/plans/
  generic/   portable — runs on EVERY backend
  libvirt/   pinned LibvirtHypervisor + libvirt device concretes
  proxmox/   pinned ProxmoxHypervisor + ProxmoxHardDrive
  esxi/      pinned ESXiHypervisor + ESXiHardDrive
```

### Generic plans

| Plan | WHAT it certifies |
|------|-------------------|
| `generic/lifecycle.py` | power-cycle churn, graceful shutdown → shutoff, reboot persistence, oversized OS-drive first-boot growth, NIC-less native-agent under churn |
| `generic/users_credentials.py` | SSH key vs password auth, multi-user privilege boundary (non-admin sudo denied), group membership, explicit per-NIC resolver |
| `generic/networking.py` | multi-`Network`-per-`Switch`, air-gap reachability matrix, NAT egress on/off, DHCP pool-boundary lease, exactly-one-default-route, cross-label DNS |
| `generic/switch_isolation.py` | the three switch tiers (uplinked / air-gapped / `mgmt` host adapter) + a provenance-pinned directional reach/isolation matrix (default route via the sidecar, mgmt reached over the `c1` leg not the NAT path, isolation by IP-literal + curl-exit-7 with a positive control); static-on-NAT sidecar-derived egress; triple-homed single-default-route |
| `generic/build_cache.py` | multi-data-disk content integrity (build→cache→run, no swap), `apt` + `pip`, post-install command ordering |
| `generic/snapshots.py` | disk snapshot create/list/restore/delete, memory snapshot restores running tmpfs state |
| `generic/concurrency.py` | independent multi-VM fan-out; run with `--jobs N` to stress parallel bring-up + teardown |

### Backend-specific plans

| Plan | WHAT it certifies | Marker backend |
|------|-------------------|----------------|
| `libvirt/devices.py` | `LibvirtOSDrive`/`LibvirtDataDrive` bus → `/dev/vd*` (virtio) vs `/dev/sd*` (sata/scsi); `LibvirtNetworkIface` `e1000e` model | libvirt |
| `libvirt/firmware_uefi.py` | `VMSpec(firmware="uefi")` OVMF boot (`/sys/firmware/efi` present) | libvirt |
| `proxmox/devices.py` | `ProxmoxHardDrive` bus → `/dev/sd*` (scsi), `/dev/vd*` (virtio) | proxmox |
| `esxi/devices.py` | `ESXiHardDrive` bus → `/dev/sd*` (scsi/sata), `/dev/nvme*` (nvme); vmdk volume format | esxi |

## Authoring conventions

- **One `PLAN` per file**, a handful of `TESTS` functions, a `TESTS = [...]`
  list, and the `if __name__ == "__main__": sys.exit(...)` runner block — mirror
  the shape of `examples/hello_world.py`.
- **Module docstring states WHAT it stresses and WHY** that edge is failure-prone
  (the two paragraphs at the top of every plan here).
- **Inline anything used once**; only hoist a module-level var/helper (a shared
  `_KEY`, an IP constant referenced by both the `PLAN` and a `TEST`, a reused
  builder helper) when it is reused more than once.
- **Generic plans stay portable** — no `LibvirtHypervisor`/concrete devices, no
  host or credentials in the plan; bind a backend at run time with `--profile`.
- **Backend-specific plans pin the driver** `Hypervisor` subclass and use that
  backend's concrete device types; document the non-obvious bus/model/firmware
  mechanics inline (these are real driver contracts, not example scaffolding).
- **`TESTS` functions take `orch: OrchestratorHandle`** and assert via
  `orch.vms[name].communicator` and `orch.driver`.
