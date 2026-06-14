"""generic/build_cache: build artifacts, the data-disk set, and package managers.

WHAT: one guest with two data disks, each seeded with disk-unique content at
build time, plus an ``apt`` package, a ``pip`` package, and a sequence of
post-install commands that append ordered markers to a file. The tests assert
each data disk carries its own content (not swapped), both are mounted, the
package managers landed their payloads, and the post-install commands ran in the
declared order.

WHY: the build → cache → run path is where a disk-set can be reordered or a seed
lost, where a cache that is not byte-stable silently re-builds, and where
post-install ordering (a guarantee range authors lean on) can be violated by a
parallelizing builder. Re-running this plan a second time should be a fast cache
hit with byte-identical results — the regression signal for the cache layer.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/build_cache.py

Run it twice to exercise warm-cache reuse: the second run must be a cache hit
(no rebuild) and produce the same green sweep.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt, Pip
from testrange.utils import SSHKey

_KEY = SSHKey.generate(comment="testrange-build-cache")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
pool1 = hyp.add_pool(StoragePool("pool1", 32))
hyp.add_switch(
    Switch(
        "lab",
        Network("lab-net"),
        cidr="10.40.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
lab_net = hyp.networks["lab-net"]
hyp.vm(
    "fileserver",
    cpu=CPU(1),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    data_disks=[HardDrive(pool1, 2), HardDrive(pool1, 2)],
    nics=[NetworkIface(lab_net, DHCPAddr())],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
        packages=[Apt("nginx"), Apt("python3-pip"), Pip("cowsay")],
        post_install_commands=(
            # Portable data-disk setup. The generic HardDrive enumerates as
            # /dev/vd* on virtio backends (libvirt, proxmox) but /dev/sd* on
            # ESXi (no virtio), so discover the two non-root whole disks
            # instead of hardcoding a node — these run as lines in one shared
            # bash script, so `set --` carries to the mkfs/mount lines. mkfs by
            # label; the run boot mounts by LABEL= (fstab below), node-agnostic.
            'os=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" | head -1)',
            'set -- $(lsblk -dno NAME --exclude 7,11 | grep -vx "$os" | sort)',
            'mkfs.ext4 -F -L data-b "/dev/$1"',
            'mkfs.ext4 -F -L data-c "/dev/$2"',
            "mkdir -p /srv/b /srv/c",
            'mount "/dev/$1" /srv/b',
            'mount "/dev/$2" /srv/c',
            "sh -c 'echo disk-b > /srv/b/which'",
            "sh -c 'echo disk-c > /srv/c/which'",
            "sh -c 'echo \"LABEL=data-b /srv/b ext4 defaults 0 2\" >> /etc/fstab'",
            "sh -c 'echo \"LABEL=data-c /srv/c ext4 defaults 0 2\" >> /etc/fstab'",
            "sh -c 'echo step-1 >> /srv/order'",
            "sh -c 'echo step-2 >> /srv/order'",
            "sh -c 'echo step-3 >> /srv/order'",
        ),
    ),
    communicator=SSHCommunicator("admin"),
)

PLAN = Plan("build-cache", hyp)


def data_disks_mounted(orch: OrchestratorHandle) -> None:
    com = orch.vms["fileserver"].communicator
    assert com.execute(["mountpoint", "-q", "/srv/b"]).ok, "/srv/b not mounted"
    assert com.execute(["mountpoint", "-q", "/srv/c"]).ok, "/srv/c not mounted"


def data_disks_carry_their_own_content(orch: OrchestratorHandle) -> None:
    com = orch.vms["fileserver"].communicator
    assert com.execute(["cat", "/srv/b/which"]).stdout.strip() == b"disk-b", "disk b/c swapped"
    assert com.execute(["cat", "/srv/c/which"]).stdout.strip() == b"disk-c", "disk b/c swapped"


def apt_package_present(orch: OrchestratorHandle) -> None:
    assert orch.vms["fileserver"].communicator.execute(["dpkg", "-l", "nginx"]).ok, "nginx missing"


def pip_package_importable(orch: OrchestratorHandle) -> None:
    r = orch.vms["fileserver"].communicator.execute(["python3", "-c", "import cowsay"])
    assert r.ok, f"pip package not importable: {r.stderr!r}"


def post_install_commands_ran_in_order(orch: OrchestratorHandle) -> None:
    r = orch.vms["fileserver"].communicator.execute(["cat", "/srv/order"])
    assert r.stdout == b"step-1\nstep-2\nstep-3\n", (
        f"post-install order not preserved: {r.stdout!r}"
    )


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    data_disks_mounted,
    data_disks_carry_their_own_content,
    apt_package_present,
    pip_package_importable,
    post_install_commands_ran_in_order,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
