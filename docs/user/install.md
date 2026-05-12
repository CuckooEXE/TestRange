# Install

`testrange` runs against a local libvirt + KVM host in v0. Remote
hypervisors and other backends (Proxmox, ESXi, Hyper-V) land later.

## Prerequisites

On Debian/Ubuntu:

```sh
sudo apt install qemu-kvm libvirt-daemon-system libvirt-dev \
                 python3-pip python3-venv
sudo usermod -a -G libvirt,kvm "$USER"
# Log out + back in for group membership to take effect.
```

On Fedora:

```sh
sudo dnf install @virtualization libvirt-devel python3-pip
sudo usermod -a -G libvirt,kvm "$USER"
```

Verify libvirt is reachable:

```sh
virsh -c qemu:///system list --all
```

`testrange` works with both `qemu:///session` (unprivileged, user-owned) and
`qemu:///system` (privileged, libvirt-qemu-owned). The default example uses
`qemu:///system`; either works once the user is in the `libvirt` group.

## Install testrange

```sh
git clone <repo-url> testrange && cd testrange
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[all,dev]'
```

## Verify

```sh
testrange --version
testrange describe examples/hello_world.py
```

The describe command should run without touching libvirt. The
``CacheEntry`` references will show "⚠ not in cache" — that's
expected until you ``testrange cache add`` them.

## Storage locations

- `$XDG_CACHE_HOME/testrange/isos/` — content-addressed cache (default:
  `~/.cache/testrange/isos/`).
- `$XDG_STATE_HOME/testrange/runs/<run_id>/` — per-run state (default:
  `~/.local/state/testrange/runs/`).
- Libvirt storage pool root — picked by the connection URI:
  - `qemu:///system` (or any remote `/system` URI): `/var/lib/libvirt/images/testrange/`
    (owned by libvirtd; the driver builds the per-pool subdirectory at pool-create time).
  - `qemu:///session`: `~/.local/share/testrange/pools/`.
  One subdirectory per pool per run.
