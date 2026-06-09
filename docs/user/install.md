# Installing testrange

`testrange` is a Python library + CLI. See [driver setup](drivers/index.md)
for the backend matrix and per-backend prerequisites.

## Requirements

- Python 3.11 or later.
- A hypervisor backend. The in-memory `MockDriver` reference backend needs
  nothing and is what the test suite drives the full lifecycle against; a clean
  live `run` of a plan needs a real backend (libvirt is the certified reference
  on local `qemu:///system`; the Proxmox and ESXi drivers are code-complete with
  live end-to-end certification in progress; Hyper-V is on the roadmap), each
  with its own prereqs — see
  [driver setup](drivers/index.md).

## Install the package

Clone, create a venv, install with the extras you need:

```sh
git clone <repo-url> testrange && cd testrange
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[all,dev]'
```

The available extras:

`ssh`
: `paramiko` — required for `SSHCommunicator` (the only network communicator
  today; `NativeCommunicator` rides the driver's guest agent and needs no
  extra).

`cloudinit`
: `pycdlib` + `pyyaml` — required for `CloudInitBuilder` (the only
  built-in builder today).

`http`
: `requests` — required for the shared HTTP cache tier (`--cache`).

`proxmox`
: `proxmoxer` + `requests` + `paramiko` + `requests-toolbelt` +
  `websocket-client` + `pycdlib` — required for the Proxmox driver.

`libvirt`
: `libvirt-python` — required for the libvirt driver (the certified reference
  backend). Imports lazily on connect, so the package registers without it.

`esxi`
: `pyvmomi` + `requests` — required for the ESXi driver. (`qemu-img`, a host
  binary, converts qcow2↔vmdk at the datastore boundary — not a wheel.)

`docs`
: `sphinx` + `furo` + `myst-parser` + `sphinx-copybutton` — to rebuild this
  documentation.

`dev`
: dev-only tools (pytest, ruff, mypy, type stubs).

`all`
: shorthand for `ssh,cloudinit,http,proxmox,libvirt,esxi`.

For a typical install you'll want `'.[all,dev]'`.

## Verify the install

```sh
testrange --version
testrange describe examples/hello_world.py
```

`describe` runs without touching any backend. The `CacheEntry` references will
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
