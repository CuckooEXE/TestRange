"""capabilities-px: Proxmox-specific capabilities, standalone.

``examples/capabilities.py`` is THE portable certification — backend-agnostic.
This file is **not** a superset of it: it stands alone and exercises features
that exist only on Proxmox, pinned to the Proxmox backend (``ProxmoxHypervisor``).
The first is per-disk controller-bus selection: a :class:`ProxmoxHardDrive` lets
a plan attach a data disk on ``scsi`` (``/dev/sd*``) or ``virtio`` (``/dev/vd*``)
and prove the guest sees it there.

Run it binding a Proxmox profile::

    testrange run --profile <proxmox> examples/capabilities-px.py
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.drivers.proxmox import ProxmoxHardDrive, ProxmoxHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "capabilities-px",
    ProxmoxHypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="multibus",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        ProxmoxHardDrive("pool1", 1, bus="scsi"),
                        ProxmoxHardDrive("pool1", 1, bus="virtio"),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    packages=[Apt("qemu-guest-agent")],
                    post_install_commands=("systemctl enable --now qemu-guest-agent",),
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def _disk_names(orch: OrchestratorHandle) -> list[str]:
    out = orch.vms["multibus"].communicator.execute(["lsblk", "-dno", "NAME"]).stdout.decode()
    return out.split()


def scsi_data_disk_presents_as_sd(orch: OrchestratorHandle) -> None:
    sd = [n for n in _disk_names(orch) if n.startswith("sd")]
    assert len(sd) >= 2, f"expected the OS + scsi data disk on /dev/sd*, got {sd!r}"


def virtio_data_disk_presents_as_vd(orch: OrchestratorHandle) -> None:
    vd = [n for n in _disk_names(orch) if n.startswith("vd")]
    assert len(vd) == 1, f"expected exactly the virtio data disk on /dev/vd*, got {vd!r}"


TESTS = [scsi_data_disk_presents_as_sd, virtio_data_disk_presents_as_vd]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
