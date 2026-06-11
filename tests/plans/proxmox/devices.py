"""proxmox/devices: Proxmox-only device concrete — per-disk controller bus.

WHAT: a Proxmox-pinned plan (``ProxmoxHypervisor``) whose guest attaches data
disks on explicit controller buses via :class:`ProxmoxHardDrive`. The tests prove
the guest-visible result: a ``scsi`` disk lands on ``/dev/sd*`` (alongside the
default-scsi OS disk) while a ``virtio`` disk lands on ``/dev/vd*``.

WHY: PVE's ``qmcreate`` disk wiring maps a plan's bus choice onto a specific
controller line (``scsi0``, ``virtio0``, …), and the import-from path that seeds
the OS disk is where bus assignments have regressed before. Certifying the
guest device node proves the bus the plan asked for is the bus the guest got.

Pinned to Proxmox::

    testrange run --profile <proxmox> tests/plans/proxmox/devices.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.drivers.proxmox import ProxmoxHardDrive, ProxmoxHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "proxmox-devices",
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
                    name="buses",
                    devices=[
                        CPU(1),
                        Memory(512),
                        # OS disk on the PVE default (scsi) -> /dev/sda.
                        OSDrive("pool1", 8),
                        # scsi data disk -> /dev/sdb; virtio data disk -> /dev/vda.
                        ProxmoxHardDrive("pool1", 1, bus="scsi"),
                        ProxmoxHardDrive("pool1", 1, bus="virtio"),
                    ],
                ),
                # NativeCommunicator agent (qemu-guest-agent) auto-provisioned (CORE-90).
                builder=CloudInitBuilder(base=CacheEntry("debian-13")),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def _disk_names(orch: OrchestratorHandle) -> list[str]:
    out = orch.vms["buses"].communicator.execute(["lsblk", "-dno", "NAME"]).stdout.decode()
    return out.split()


def scsi_disks_present_as_sd(orch: OrchestratorHandle) -> None:
    sd = [n for n in _disk_names(orch) if n.startswith("sd")]
    assert len(sd) >= 2, f"expected the OS + scsi data disk on /dev/sd*, got {sd!r}"


def virtio_disk_presents_as_vd(orch: OrchestratorHandle) -> None:
    vd = [n for n in _disk_names(orch) if n.startswith("vd")]
    assert len(vd) == 1, f"expected exactly the virtio data disk on /dev/vd*, got {vd!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    scsi_disks_present_as_sd,
    virtio_disk_presents_as_vd,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
