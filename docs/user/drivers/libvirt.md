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
