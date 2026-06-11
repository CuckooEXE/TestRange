"""libvirt/devices: libvirt-only device concretes — disk controller bus + NIC model.

WHAT: a libvirt-pinned plan (``LibvirtHypervisor``) whose guest attaches disks on
explicit controller buses via :class:`LibvirtOSDrive` / :class:`LibvirtDataDrive`
and a NIC emulated as a chosen model via :class:`LibvirtNetworkIface`. The tests
prove the guest-visible result: virtio-blk disks land on ``/dev/vd*`` while
sata/scsi disks land on ``/dev/sd*``, and the NIC is bound by the ``e1000e``
driver rather than the default ``virtio-net``.

WHY: these concretes exist precisely because the controller a disk hangs off and
the emulated NIC model are libvirt knobs the portable types cannot express (the
motivating case is a nested ESXi guest, which has no virtio drivers at all). The
``_vm.py`` per-bus device allocator (``vd*``/``sd*``) is exactly the kind of
mapping that silently regresses — a sata OS disk colliding with a sata seed
CDROM, or the build NIC's model leaking onto the run NIC. This certifies the
guest sees what the plan declared.

Pinned to libvirt (using a concrete here binds the plan to the libvirt backend)::

    testrange run --profile <libvirt> tests/plans/libvirt/devices.py

NOTE: an ``ide`` bus is a valid ``LibvirtDataDrive`` choice, but modern Debian
routes PATA/IDE through ``libata`` and presents it as ``/dev/sd*`` (not the
historical ``/dev/hd*``), so this plan certifies the unambiguous virtio-vs-sd
split rather than asserting an ``hd*`` node that the guest kernel no longer emits.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, StoragePool
from testrange.devices.disk.libvirt import LibvirtDataDrive, LibvirtOSDrive
from testrange.devices.network import DHCPAddr
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "libvirt-devices",
    LibvirtHypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "lab",
                Network("lab-net"),
                cidr="10.40.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="buses",
                    devices=[
                        CPU(1),
                        Memory(512),
                        # virtio OS disk -> /dev/vda; a second virtio data disk -> /dev/vdb.
                        LibvirtOSDrive("pool1", 8, bus="virtio"),
                        LibvirtDataDrive("pool1", 1, bus="virtio"),
                        # sata + scsi data disks both present to the guest as /dev/sd*.
                        LibvirtDataDrive("pool1", 1, bus="sata"),
                        LibvirtDataDrive("pool1", 1, bus="scsi"),
                        # Emulated as an Intel e1000e card rather than virtio-net.
                        LibvirtNetworkIface("lab-net", model="e1000e", addr=DHCPAddr()),
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


def virtio_disks_present_as_vd(orch: OrchestratorHandle) -> None:
    vd = [n for n in _disk_names(orch) if n.startswith("vd")]
    assert len(vd) == 2, f"expected the virtio OS + virtio data disk on /dev/vd*, got {vd!r}"


def sata_and_scsi_disks_present_as_sd(orch: OrchestratorHandle) -> None:
    sd = [n for n in _disk_names(orch) if n.startswith("sd")]
    assert len(sd) == 2, f"expected the sata + scsi data disks on /dev/sd*, got {sd!r}"


def nic_is_bound_by_e1000e_driver(orch: OrchestratorHandle) -> None:
    drivers = orch.vms["buses"].communicator.execute(
        [
            "sh",
            "-c",
            "for d in /sys/class/net/*; do n=$(basename $d); [ $n = lo ] && continue; "
            "basename $(readlink -f $d/device/driver); done",
        ]
    )
    assert b"e1000e" in drivers.stdout, f"run NIC not bound by e1000e: {drivers.stdout!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    virtio_disks_present_as_vd,
    sata_and_scsi_disks_present_as_sd,
    nic_is_bound_by_e1000e_driver,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
