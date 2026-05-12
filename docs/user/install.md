# Installing testrange

`testrange` is a Python library + CLI. To run tests it needs at least one
hypervisor backend installed; see [driver install](drivers/index.md) for
backend prerequisites.

## Requirements

- Python 3.11 or later.
- A hypervisor backend (only [libvirt + KVM](drivers/libvirt.md) is shipped
  today; ESXi / Proxmox / Hyper-V drivers are on the long-term roadmap).

## Install the package

Clone, create a venv, install with the extras you need:

```sh
git clone <repo-url> testrange && cd testrange
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[all,dev]'
```

The available extras:

`libvirt`
: `libvirt-python` — required to talk to libvirtd.

`ssh`
: `paramiko` — required for `SSHCommunicator` (the only built-in
  communicator today).

`cloudinit`
: `pycdlib` + `pyyaml` — required for `CloudInitBuilder` (the only
  built-in builder today).

`docs`
: `sphinx` + `furo` + `myst-parser` — to rebuild this documentation.

`dev`
: dev-only tools (pytest, ruff, mypy, type stubs).

`all`
: shorthand for `libvirt,ssh,cloudinit`.

For a typical install you'll want `'.[all,dev]'`.

## Verify the install

```sh
testrange --version
testrange describe examples/hello_world.py
```

`describe` runs without touching libvirt. The `CacheEntry` references will
show "⚠ not in cache" until you populate the cache. That's the next step
in the per-driver setup pages.

## Storage locations

testrange writes to three XDG-style locations:

- `$XDG_CACHE_HOME/testrange/isos/` — content-addressed cache (default:
  `~/.cache/testrange/isos/`). One `<sha>.bin` per disk + sidecar
  `<sha>.json`.
- `$XDG_STATE_HOME/testrange/runs/<run_id>/` — per-run state file +
  PID file (default: `~/.local/state/testrange/runs/`).
- A hypervisor-side **pool root** for VM disks (path picked by the driver
  — see the per-driver page).
