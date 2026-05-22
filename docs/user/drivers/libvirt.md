# libvirt + KVM

The libvirt driver targets a local (or remote-via-SSH) libvirtd. It
supports both `qemu:///system` (privileged, libvirt-qemu-owned) and
`qemu:///session` (unprivileged, user-owned). The shipped
[`examples/hello_world.py`](https://github.com/) uses `qemu:///system`.

## Host prerequisites

### Debian / Ubuntu

```sh
sudo apt install qemu-kvm libvirt-daemon-system libvirt-dev \
                 python3-pip python3-venv
sudo usermod -a -G libvirt,kvm "$USER"
# Log out + back in for group membership to take effect.
```

### Fedora / RHEL

```sh
sudo dnf install @virtualization libvirt-devel python3-pip
sudo usermod -a -G libvirt,kvm "$USER"
```

## Verify libvirt is reachable

```sh
virsh -c qemu:///system list --all
```

testrange works with both `qemu:///system` and `qemu:///session`; the
shipped example uses `system`. Either works once your user is in the
`libvirt` group.

## Storage pool root

The libvirt driver picks the pool root from the connection URI:

- `qemu:///system` (or any remote `/system` URI): `/var/lib/libvirt/images/testrange/`
  (libvirtd owns this; the driver builds per-pool subdirectories at
  pool-create time).
- `qemu:///session`: `~/.local/share/testrange/pools/`.

One subdirectory per pool per run. Teardown removes both the volumes
and the per-pool directory (including any leftover snapshot files).

## Cache a base disk

testrange refuses to bring up a VM until its base disk is in the cache.
Pull a Debian cloud image (any qcow2 will do; this is just an example):

```sh
testrange cache add \
    https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
    --name debian-13
testrange cache list
```

The CacheEntry in the plan (`CacheEntry("debian-13")`) resolves to that
content-addressed entry.

## VM-control console

Every VM gets a VNC graphics device on `listen='127.0.0.1'` with
virtio-gpu. `virt-viewer <domain>` on the libvirtd host attaches to
the running VM; `virt-viewer -c qemu+ssh://user@host/system <domain>`
from a remote workstation works too. Useful for debugging
`--leak-on-failure` runs.

## Networking

The libvirt driver implements every `Switch` flag itself — no
libvirt-native NAT, DHCP, or DNS is used anywhere. See
[Networking modes](networking-modes.md) for the full per-flag mapping;
the short version:

- `uplink=` and `mgmt=True` are realized via host bridges created with
  **pyroute2** (`tr-<10-hex-sha256>`). pyroute2 talks LOCAL netlink
  only — remote libvirt URIs (`qemu+ssh://...`) combined with any
  `Switch` that requires a bridge fail preflight with
  `remote_uplink_unsupported`.
- `dhcp=True`, `dns=True`, `nat=True` are realized by the per-Switch
  **sidecar VM**: a pre-built Alpine image with `dnsmasq`,
  `nftables`, and `qemu-guest-agent`. Build with
  `sudo ./tools/build-sidecar-image/build.sh` and cache it once via
  `testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2
  --name testrange-sidecar`.
- The build phase requires `build_uplink="<nic>"` on
  `LibvirtHypervisor` when any VM is a cache miss; the orchestrator
  synthesizes a transient build Switch (`10.97.99.0/24`,
  dhcp+dns+nat) on that uplink, brings up its sidecar, runs the
  build VMs, and tears it all down LIFO before the run phase.

### Sidecar image build

```sh
sudo ./tools/build-sidecar-image/build.sh
.venv/bin/python -m testrange.cli cache add \
    tools/build-sidecar-image/testrange-sidecar.qcow2 \
    --name testrange-sidecar
```

The build uses `alpine-make-vm-image` (requires root for chroot).
Bakes in `dnsmasq + nftables + qemu-guest-agent + blkid + openrc`
and an OpenRC `testrange-sidecar` service that runs in the `boot`
runlevel: mounts the `TR_SIDECAR_CFG`-labeled ISO and copies the
four rendered config files into place before the networking,
dnsmasq, and nftables services start.

### Bridge naming + capabilities

`LibvirtDriver.compose_bridge_name(run_id, switch_name)` returns
`tr-<10-hex-sha256>` — 13 characters, well under Linux's 15-char
`IFNAMSIZ` limit. Bridges are recorded in `state.json` as
`kind="bridge"` so LIFO teardown removes them automatically; if
teardown is killed mid-flight, `testrange cleanup <run_id>` will
still drop them.

Creating a bridge needs `CAP_NET_ADMIN`. The driver wraps a
pyroute2 `EPERM` into `DriverError("...: needs CAP_NET_ADMIN (run
as root, or grant the cap)")` so the failure mode is obvious.
