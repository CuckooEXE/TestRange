"""data_disk: a HardDrive seeded at build, served at run.

The build VM boots with both its OS disk and a blank data disk attached; the
cloud-init payload formats the data disk and writes content onto it. testrange
captures *both* disks into the cache, so the run VM comes up with the data disk
already populated.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange build examples/data_disk.py
    testrange run examples/data_disk.py
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import (
    CPU,
    HardDrive,
    Memory,
    OSDrive,
    StoragePool,
)
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-data-disk")

PLAN = Plan(
    "data-disk",
    ProxmoxHypervisor(
        host="40.160.34.83",
        password="Target123!",
        build_switch=ManagedBuildSwitch(uplink="vmbr0"),
        networks=[
            Switch(
                "switch1",
                Network("netA"),
                cidr="172.31.0.0/24",
                uplink="vmbr9",
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("pool1", 64)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="fileserver",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        HardDrive("pool1", 16),
                        NetworkIface("netA", addr=StaticAddr("172.31.0.150")),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred("myuser", password="mypass", ssh_key=_KEY, admin=True),
                    ],
                    post_install_commands=(
                        "mkfs.ext4 -F /dev/vdb",
                        "mkdir -p /srv/data",
                        "mount /dev/vdb /srv/data",
                        "echo served-at-run > /srv/data/index.html",
                        "echo '/dev/vdb /srv/data ext4 defaults 0 2' >> /etc/fstab",
                    ),
                ),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)


def data_disk_is_mounted(orch: OrchestratorHandle) -> None:
    r = orch.vms["fileserver"].communicator.execute(["mountpoint", "-q", "/srv/data"])
    assert r.exit_code == 0, "data disk not mounted at /srv/data"


def seeded_content_present(orch: OrchestratorHandle) -> None:
    r = orch.vms["fileserver"].communicator.execute(["cat", "/srv/data/index.html"])
    assert r.exit_code == 0, "seeded file missing from the data disk"


TESTS = [data_disk_is_mounted, seeded_content_present]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
