"""generic/firmware: UEFI firmware boot, on every backend.

WHAT: a backend-agnostic guest declared with ``hyp.vm(..., firmware="uefi")``, booted
on the backend's UEFI firmware rather than its default BIOS. The tests assert the
guest came up in UEFI mode — ``/sys/firmware/efi`` is present only when the kernel
booted via EFI, and the efivars filesystem is populated.

WHY: all three backends advertise ``uefi`` in ``SUPPORTED_FIRMWARES``, but the
firmware seam drives a different VM shape per backend (OVMF loader + NVRAM on
libvirt, ``efidisk0``/``efitype`` on Proxmox, an EFI firmware on ESXi) and a
different boot path. A guest that silently falls back to BIOS still boots, so only
an in-guest assertion catches the regression — and this being a *generic* plan is
what certifies UEFI on every backend (CLAUDE.md §4), not just the libvirt
reference (see also tests/plans/libvirt/firmware_uefi.py).

Runs on every backend::

    testrange run --profile <name> tests/plans/generic/firmware.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch

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
    "uefi",
    cpu=CPU(1),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(lab_net, DHCPAddr())],
    firmware="uefi",
    # NativeCommunicator agent auto-provisioned per backend (CORE-90);
    # the PosixCred is for ESXi VMware Tools guest-ops (CORE-60).
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
    ),
    communicator=NativeCommunicator(),
)

PLAN = Plan("firmware", hyp)


def booted_in_uefi_mode(orch: OrchestratorHandle) -> None:
    r = orch.vms["uefi"].communicator.execute(["test", "-d", "/sys/firmware/efi"])
    assert r.ok, "guest did not boot via UEFI (/sys/firmware/efi absent — fell back to BIOS?)"


def efivars_are_populated(orch: OrchestratorHandle) -> None:
    r = orch.vms["uefi"].communicator.execute(["ls", "/sys/firmware/efi/efivars"])
    assert r.ok and r.stdout.strip(), f"efivars filesystem empty or absent: {r.stdout!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    booted_in_uefi_mode,
    efivars_are_populated,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
