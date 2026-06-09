"""esxi/devices: ESXi-only device concrete — per-disk controller bus + vmdk.

WHAT: an ESXi-pinned plan (``ESXiHypervisor``) whose guest attaches data disks on
explicit controller buses via :class:`ESXiHardDrive`. The tests prove the
guest-visible result: ``scsi`` and ``sata`` disks land on ``/dev/sd*`` while an
``nvme`` disk lands on ``/dev/nvme*``. The guest is reached over the native
VMware Tools guest-ops channel (no NIC required), which also certifies the
qcow2 → vmdk inflate that backs every ESXi volume (a guest that boots and answers
proves the canonical-qcow2 cache image was correctly converted to a runnable vmdk).

WHY: ESXi has no virtio at all, so the controller bus is the only disk-shape knob
and the one most likely to regress in the ``_vm.py`` controller wiring. The nvme
path in particular is distinct hardware on the VM and a separate guest driver.
Certifying the guest device node proves the bus the plan asked for is the bus the
guest got, end to end across the vmdk boundary.

Pinned to ESXi (firmware defaults to ``bios``, the certified path)::

    testrange run --profile <esxi> tests/plans/esxi/devices.py

The guest needs the VMware Tools vix plugin for guest-ops exec, so the build
installs ``open-vm-tools-plugins-all`` (the base ``open-vm-tools`` omits it).
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.drivers.esxi import ESXiHypervisor
from testrange.drivers.esxi.devices import ESXiHardDrive
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "esxi-devices",
    ESXiHypervisor(
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
                        Memory(1024),
                        # OS disk on the ESXi default (scsi) -> /dev/sda.
                        OSDrive("pool1", 8),
                        # scsi + sata data disks present to the guest as /dev/sd*.
                        ESXiHardDrive("pool1", 1, bus="scsi"),
                        ESXiHardDrive("pool1", 1, bus="sata"),
                        # nvme data disk presents as /dev/nvme*.
                        ESXiHardDrive("pool1", 1, bus="nvme"),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[PosixCred("admin", password="TestRangeEsxi2026!", admin=True)],
                    packages=[Apt("open-vm-tools-plugins-all")],
                    post_install_commands=("systemctl enable --now open-vm-tools",),
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def _disk_names(orch: OrchestratorHandle) -> list[str]:
    out = orch.vms["buses"].communicator.execute(["lsblk", "-dno", "NAME"]).stdout.decode()
    return out.split()


def scsi_and_sata_disks_present_as_sd(orch: OrchestratorHandle) -> None:
    # Exactly three: OS (scsi) + scsi data + sata data. An exact count is what
    # certifies the bus split — `>= 3` would still pass if the nvme disk wrongly
    # enumerated as /dev/sd*, defeating the very distinction this plan stresses.
    sd = [n for n in _disk_names(orch) if n.startswith("sd")]
    assert len(sd) == 3, f"expected exactly OS + scsi + sata disks on /dev/sd*, got {sd!r}"


def nvme_disk_presents_as_nvme(orch: OrchestratorHandle) -> None:
    nvme = [n for n in _disk_names(orch) if n.startswith("nvme")]
    assert len(nvme) == 1, (
        f"expected exactly the one nvme data disk on /dev/nvme*, got {nvme!r} "
        f"(all disks {_disk_names(orch)!r})"
    )


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    scsi_and_sata_disks_present_as_sd,
    nvme_disk_presents_as_nvme,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
