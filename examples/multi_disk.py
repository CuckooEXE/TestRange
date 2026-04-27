"""A VM with two disks: primary root + a separate data volume.

The first ``HardDrive`` in a VM's ``devices=[...]`` list is **always
the OS disk** — cloud-init installs onto it and the post-install
snapshot is what lands in the cache.  Any additional ``HardDrive``
entries become empty data volumes (``<vm>-data<n>.<ext>`` in the
per-run scratch dir, where ``<ext>`` is the backend's native disk
format) and are ephemeral — each run starts with a blank volume.

This example also demonstrates the ergonomic numeric form
(``HardDrive(20)`` → 20 GiB) alongside the string form
(``HardDrive("5GB")``) — both are equivalent; pick whichever reads
better.

The guest sees additional disks as ``/dev/vdb``, ``/dev/vdc``, etc.
This test partitions and mounts the data volume, writes a file,
unmounts it, and re-mounts to prove the data survives.

Run with::

    testrange run examples/multi_disk.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)


def exercise_data_disk(orch: Orchestrator) -> None:
    vm = orch.vms["storage"]

    # Guest should see vda (root) + vdb (data).
    lsblk = vm.exec(["lsblk", "-dno", "NAME,SIZE"]).check()
    names = lsblk.stdout_text.split()
    assert "vda" in names, lsblk.stdout_text
    assert "vdb" in names, lsblk.stdout_text

    # Format and mount the data disk.
    vm.exec(["mkfs.ext4", "-q", "-F", "/dev/vdb"]).check()
    vm.exec(["mkdir", "-p", "/srv/data"]).check()
    vm.exec(["mount", "/dev/vdb", "/srv/data"]).check()

    # Write, unmount, re-mount, verify survival.
    vm.write_text("/srv/data/canary.txt", "still here\n")
    vm.exec(["umount", "/srv/data"]).check()
    vm.exec(["mount", "/dev/vdb", "/srv/data"]).check()
    assert vm.read_text("/srv/data/canary.txt") == "still here\n"


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.16.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="storage",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        pkgs=[Apt("e2fsprogs")],
                        devices=[
                            vCPU(2),
                            Memory(2),
                            vNIC("Net"),
                            # First HardDrive is always the OS disk.
                            HardDrive(20),  # 20 GiB primary (root filesystem)
                            HardDrive(5),   # 5 GiB data disk → /dev/vdb
                        ],
                    ),
                ],
            ),
            exercise_data_disk,
            name="multi-disk",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
