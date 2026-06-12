"""generic/snapshots: disk and memory snapshot lifecycle.

WHAT: one guest taken through the full disk-snapshot lifecycle — create, list,
restore-discards-a-later-write, delete — and a memory snapshot that captures live
RAM state and restores the guest **running**, with a tmpfs sentinel that only RAM
could have preserved.

WHY: snapshots are the most stateful thing a driver does, and the easiest to get
subtly wrong: a restore that boots the guest instead of resuming it, a disk
snapshot that does not actually roll back a later write, a list that omits the
snapshot, a delete that leaves the chain dangling. The memory-snapshot test is
the sharp one — a ``/dev/shm`` sentinel written after the snap and removed before
the restore can only reappear if running state was truly captured to and from RAM.

Portable — bind a backend at run time (the bound backend must support memory
snapshots)::

    testrange run --profile <name> tests/plans/generic/snapshots.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-snapshots")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
hyp.add_pool(StoragePool("pool1", 32))
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
hyp.add_vm(
    VMRecipe(
        spec=VMSpec(
            name="snapbox",
            devices=[
                CPU(2),
                Memory(1024),
                OSDrive(hyp.pools["pool1"], 8),
                NetworkIface(hyp.networks["lab-net"], addr=DHCPAddr()),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
        ),
        communicator=SSHCommunicator("admin"),
    )
)

PLAN = Plan("snapshots", hyp)


def disk_snapshot_lifecycle(orch: OrchestratorHandle) -> None:
    vm = orch.vms["snapbox"]
    driver = orch.driver
    com = vm.communicator
    sentinel = "/home/admin/snapshot-test"

    driver.create_snapshot(vm.backend_name, "pre-write", "before sentinel")
    com.execute(["touch", sentinel])
    assert com.execute(["test", "-f", sentinel]).ok, "sentinel not created"

    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    assert driver.get_vm_power_state(vm.backend_name) == "shutoff", "VM did not power off"
    driver.start_vm(vm.backend_name)
    com.close()
    assert com.execute(["test", "-f", sentinel]).ok, "sentinel lost across reboot"

    assert "pre-write" in driver.list_snapshots(vm.backend_name), "snapshot not listed"

    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    driver.restore_snapshot(vm.backend_name, "pre-write")
    driver.start_vm(vm.backend_name)
    com.close()
    assert not com.execute(["test", "-f", sentinel]).ok, "sentinel survived restore"

    driver.delete_snapshot(vm.backend_name, "pre-write")
    assert "pre-write" not in driver.list_snapshots(vm.backend_name), "snapshot not deleted"


def memory_snapshot_restores_running_state(orch: OrchestratorHandle) -> None:
    vm = orch.vms["snapbox"]
    driver = orch.driver
    com = vm.communicator
    marker = "/dev/shm/mem-marker"

    com.execute(["sh", "-c", f"echo live > {marker}"])
    driver.create_snapshot(vm.backend_name, "mem-snap", "running state", mem=True)
    com.execute(["rm", "-f", marker])

    driver.restore_snapshot(vm.backend_name, "mem-snap")
    assert driver.get_vm_power_state(vm.backend_name) == "running", "mem restore left VM down"
    com.close()
    r = com.execute(["cat", marker])
    assert r.stdout.strip() == b"live", f"tmpfs state not restored from RAM snapshot: {r}"

    driver.delete_snapshot(vm.backend_name, "mem-snap")


def memory_snapshot_on_shutoff_vm_is_rejected(orch: OrchestratorHandle) -> None:
    # ABC contract (REL-34): mem=True on a powered-off VM has no RAM to capture,
    # so it must raise rather than silently degrade to a disk-only snapshot.
    vm = orch.vms["snapbox"]
    driver = orch.driver
    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    assert driver.get_vm_power_state(vm.backend_name) == "shutoff", "VM did not power off"
    try:
        raised = False
        try:
            driver.create_snapshot(vm.backend_name, "mem-on-off", mem=True)
        except DriverError:
            raised = True
    finally:
        driver.start_vm(vm.backend_name)
        vm.communicator.close()
    assert raised, "mem=True on a shut-off VM must raise, not silently disk-only"
    assert "mem-on-off" not in driver.list_snapshots(vm.backend_name), "rejected snapshot persisted"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    disk_snapshot_lifecycle,
    memory_snapshot_restores_running_state,
    memory_snapshot_on_shutoff_vm_is_rejected,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
