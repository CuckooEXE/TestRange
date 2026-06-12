"""generic/snapshot_chain: multi-snapshot chains, data-disk revert, contract edges.

WHAT: one guest with its OS disk and a data disk in two *different* storage
pools, taken through a two-snapshot chain — both snapshots created while the
guest is shut off — then restored to the middle, forward to the newest, and
pruned. Alongside the chain walk: a duplicate snapshot name must be rejected,
restoring a missing name must raise, deleting a missing name must not, and a
memory snapshot must restore *running* state onto a guest that was powered off.

WHY: ``generic/snapshots.py`` certifies one snapshot at a time on a single-disk
VM, which leaves the sharpest driver edges uncovered: a ``list_snapshots`` that
loses oldest-first order (the chain is deliberately named so lexicographic
order is the REVERSE of creation order — a name-sorting driver fails here), a
revert that silently skips the data disk (or the second pool), a delete that
corrupts the rest of the chain, and name-collision handling that quietly
overwrites. The snapshots here are taken shut off because that is the one shape
every backend treats identically as disk-only — libvirt escalates any
running-domain snapshot to a full RAM checkpoint — which is also what makes the
post-restore ``shutoff`` power state assertable portably, and what makes the
duplicate-name probe a minimal pair with the creates it collides with.

The TESTS form one ordered chain; a mid-chain failure can cascade, so the
contract-edge probes assert their preconditions with distinct messages.

Portable — bind a backend at run time (the bound backend must support memory
snapshots)::

    testrange run --profile <name> tests/plans/generic/snapshot_chain.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.exceptions import DriverError
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
# Two pools on purpose: the OS disk and the data disk live in different
# pools, so a revert that only walks the OS disk's pool shows up here.
hyp.add_pool(StoragePool("os-pool", 24))
hyp.add_pool(StoragePool("data-pool", 8))
hyp.add_switch(
    Switch(
        "lab",
        Network("lab-net"),
        cidr="10.64.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
hyp.add_vm(
    VMRecipe(
        spec=VMSpec(
            name="chainbox",
            devices=[
                CPU(2),
                Memory(1024),
                OSDrive(hyp.pools["os-pool"], 8),
                HardDrive(hyp.pools["data-pool"], 2),
                NetworkIface(hyp.networks["lab-net"], addr=DHCPAddr()),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            # The PosixCred is for ESXi VMware Tools guest-ops, which
            # authenticate per call (CORE-60); QGA backends ignore it.
            credentials=[PosixCred("admin", password="testrange", admin=True)],
            post_install_commands=(
                # Portable data-disk setup (the ESXI-27 lesson): discover
                # the one non-root whole disk instead of hardcoding a
                # node — /dev/vd* on virtio backends, /dev/sd* on ESXi.
                # These run as lines of one shared bash script, so
                # `set --` carries to the mkfs line. chmod 0777 lands in
                # the filesystem root so the run-phase guest user (ESXi
                # guest-ops run as `admin`, not root) can write markers.
                'os=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" | head -1)',
                'set -- $(lsblk -dno NAME --exclude 7,11 | grep -vx "$os" | sort)',
                'mkfs.ext4 -F -L chain-data "/dev/$1"',
                "mkdir -p /data",
                "mount LABEL=chain-data /data",
                "chmod 0777 /data",
                "sh -c 'echo \"LABEL=chain-data /data ext4 defaults 0 2\" >> /etc/fstab'",
            ),
        ),
        communicator=NativeCommunicator(),
    )
)

PLAN = Plan("snapshot-chain", hyp)

# The same epoch value is written to the OS disk and the data disk before each
# snapshot, so a restore that reverts one disk but not the other is caught by
# comparing the two readbacks. Chain names are chosen so lexicographic order
# REVERSES creation order ("epoch-new" < "epoch-old"): a list_snapshots that
# sorts by name instead of creation time cannot pass the ordering asserts.
_OS_MARKER = "/home/admin/epoch"
_DATA_MARKER = "/data/epoch"


def _write_epoch(orch: OrchestratorHandle, value: str) -> None:
    com = orch.vms["chainbox"].communicator
    for path in (_OS_MARKER, _DATA_MARKER):
        # sync: the write must be on disk before the imminent shutdown — a slow
        # guest whose graceful shutdown overruns into the driver's hard-stop
        # would otherwise lose the page-cached marker and surface it two tests
        # later as a baffling restore mismatch.
        r = com.execute(["sh", "-c", f"echo {value} > {path} && sync"])
        assert r.ok, f"writing epoch marker {path} failed: {r}"


def _read_epoch(orch: OrchestratorHandle) -> tuple[str, str]:
    com = orch.vms["chainbox"].communicator
    out = []
    for path in (_OS_MARKER, _DATA_MARKER):
        r = com.execute(["cat", path])
        out.append(r.stdout.decode().strip() if r.ok else f"<unreadable: {path}>")
    return out[0], out[1]


def _stop(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    orch.driver.shutdown_vm(vm.backend_name, timeout=120.0)
    state = orch.driver.get_vm_power_state(vm.backend_name)
    assert state == "shutoff", f"chainbox did not power off: {state}"


def _boot(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    orch.driver.start_vm(vm.backend_name)
    vm.communicator.close()
    assert vm.communicator.execute(["true"]).ok, "chainbox unreachable after start"


def chain_of_shutoff_snapshots_lists_oldest_first(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    # Positive control before anything else: /data must actually be the mounted
    # data disk. On QGA backends the agent runs as root, so marker writes into
    # an UNMOUNTED /data directory would succeed silently and every revert
    # assert would certify single-disk revert only.
    r = vm.communicator.execute(["findmnt", "-no", "SOURCE", "/data"])
    assert r.ok and r.stdout.strip(), "/data is not a mountpoint — data-disk coverage void"
    _write_epoch(orch, "one")
    _stop(orch)
    orch.driver.create_snapshot(vm.backend_name, "epoch-old", "first epoch")
    _boot(orch)
    _write_epoch(orch, "two")
    _stop(orch)
    orch.driver.create_snapshot(vm.backend_name, "epoch-new", "second epoch")
    _boot(orch)
    snaps = orch.driver.list_snapshots(vm.backend_name)
    assert snaps == ["epoch-old", "epoch-new"], (
        f"chain not listed oldest-first (creation order, not name order): {snaps}"
    )


def duplicate_snapshot_name_is_rejected(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    snaps = orch.driver.list_snapshots(vm.backend_name)
    assert "epoch-old" in snaps, f"precondition from the chain test not met (cascade): {snaps}"
    # Probe while shut off — the identical shape the chain's successful creates
    # used — so the name collision is the ONLY differing variable; a running
    # probe could be rejected for the power state instead (libvirt escalates a
    # running create to a RAM checkpoint, and the ABC lets a backend refuse it).
    _stop(orch)
    try:
        raised = None
        try:
            orch.driver.create_snapshot(vm.backend_name, "epoch-old", "collision")
        except DriverError as e:
            raised = e
    finally:
        _boot(orch)
    assert raised is not None, "duplicate snapshot name must raise, not overwrite"
    assert "already exists" in str(raised), f"wrong rejection cause: {raised}"
    snaps = orch.driver.list_snapshots(vm.backend_name)
    assert snaps == ["epoch-old", "epoch-new"], f"rejected duplicate mutated the chain: {snaps}"


def restore_to_the_middle_rolls_back_both_disks(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    _stop(orch)
    orch.driver.restore_snapshot(vm.backend_name, "epoch-old")
    state = orch.driver.get_vm_power_state(vm.backend_name)
    assert state == "shutoff", f"disk-only restore must leave the VM shutoff: {state}"
    _boot(orch)
    epochs = _read_epoch(orch)
    assert epochs == ("one", "one"), f"restore to epoch-old did not roll back both disks: {epochs}"


def restore_forward_returns_the_newer_state(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    _stop(orch)
    orch.driver.restore_snapshot(vm.backend_name, "epoch-new")
    _boot(orch)
    epochs = _read_epoch(orch)
    assert epochs == ("two", "two"), (
        f"restore forward to epoch-new did not return the newer state: {epochs}"
    )


def restore_of_a_missing_snapshot_raises(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    raised = None
    try:
        orch.driver.restore_snapshot(vm.backend_name, "no-such-epoch")
    except DriverError as e:
        raised = e
    assert raised is not None, "restoring a missing snapshot must raise"
    assert "not found" in str(raised), f"wrong rejection cause: {raised}"
    # The probe ran against a RUNNING VM on purpose: a driver that resolves a
    # miss by reverting to the nearest snapshot would disturb the power state.
    state = orch.driver.get_vm_power_state(vm.backend_name)
    assert state == "running", f"failed restore must not disturb the VM: {state}"


def delete_of_a_missing_snapshot_is_a_noop(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    before = orch.driver.list_snapshots(vm.backend_name)
    assert "epoch-old" in before, f"precondition from the chain test not met (cascade): {before}"
    orch.driver.delete_snapshot(vm.backend_name, "no-such-epoch")
    snaps = orch.driver.list_snapshots(vm.backend_name)
    assert snaps == before, f"missing-name delete mutated the chain: {before} -> {snaps}"


def deleting_the_oldest_leaves_the_newest_restorable(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    orch.driver.delete_snapshot(vm.backend_name, "epoch-old")
    snaps = orch.driver.list_snapshots(vm.backend_name)
    assert snaps == ["epoch-new"], f"chain after deleting the oldest: {snaps}"
    _write_epoch(orch, "scratch")
    _stop(orch)
    orch.driver.restore_snapshot(vm.backend_name, "epoch-new")
    _boot(orch)
    epochs = _read_epoch(orch)
    assert epochs == ("two", "two"), (
        f"epoch-new not restorable after its predecessor was deleted: {epochs}"
    )


def memory_snapshot_restores_running_state_onto_a_shutoff_vm(orch: OrchestratorHandle) -> None:
    vm = orch.vms["chainbox"]
    com = vm.communicator
    marker = "/dev/shm/chain-mem"
    r = com.execute(["sh", "-c", f"echo live > {marker}"])
    assert r.ok, f"writing tmpfs marker failed: {r}"
    orch.driver.create_snapshot(vm.backend_name, "ram-epoch", "running state", mem=True)
    _stop(orch)
    orch.driver.restore_snapshot(vm.backend_name, "ram-epoch")
    state = orch.driver.get_vm_power_state(vm.backend_name)
    assert state == "running", f"memory restore onto a shutoff VM must resume it: {state}"
    com.close()
    r = com.execute(["cat", marker])
    assert r.stdout.strip() == b"live", f"tmpfs state not restored from the RAM snapshot: {r}"
    orch.driver.delete_snapshot(vm.backend_name, "ram-epoch")


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    chain_of_shutoff_snapshots_lists_oldest_first,
    duplicate_snapshot_name_is_rejected,
    restore_to_the_middle_rolls_back_both_disks,
    restore_forward_returns_the_newer_state,
    restore_of_a_missing_snapshot_raises,
    delete_of_a_missing_snapshot_is_a_noop,
    deleting_the_oldest_leaves_the_newest_restorable,
    memory_snapshot_restores_running_state_onto_a_shutoff_vm,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
