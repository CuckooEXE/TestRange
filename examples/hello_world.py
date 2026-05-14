"""hello_world: one libvirt VM, cloud-init bootstraps SSH + nginx, smoke-test it.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13

Usage:
    testrange describe examples/hello_world.py
    testrange run examples/hello_world.py
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred, SSHKey
from testrange.devices import (
    CPU,
    Memory,
    OSDrive,
    StoragePool,
)
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.drivers.libvirt import LibvirtHypervisor
from testrange.networks import Network, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-hello")

PLAN = Plan(
    LibvirtHypervisor(
        connection="qemu:///system",
        networks=[
            Switch(
                "switch1",
                Network("netA", "172.31.0.0/24", dhcp=True, dns=True),
                Network("netB", "10.10.10.0/24"),
                mgmt=True,
                internet=True,
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="web",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        LibvirtNetworkIface("netA", driver="virtio", ipv4="172.31.0.59"),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[
                        PosixCred("root", password="root"),
                        PosixCred(
                            "myuser",
                            password="mypass",
                            pubkey=_KEY.auth_line,
                            privkey=_KEY.priv,
                            sudo=True,
                        ),
                    ],
                    packages=[Apt("nginx")],
                    post_install_commands=("echo hi > /tmp/hi",),
                ),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)


def nginx_is_installed(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["dpkg", "-l", "nginx"])
    assert r.exit_code == 0, "nginx missing"


def hostname_matches(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["hostname"])
    assert r.stdout.strip() == b"web", r


def snapshot_lifecycle(orch: OrchestratorHandle) -> None:
    vm = orch.vms["web"]
    driver = orch.driver
    vm_be = vm.backend_name
    com = vm.communicator
    sentinel = "/home/myuser/snapshot-test.txt"

    driver.create_snapshot(vm_be, "pre-write", "before sentinel file")

    com.execute(["touch", sentinel])
    r = com.execute(["test", "-f", sentinel])
    assert r.ok, f"sentinel not created: {r}"

    # Reboot via driver: shutdown waits for shutoff, then start.
    driver.shutdown_vm(vm_be, timeout=60.0)
    driver.start_vm(vm_be)
    com.close()  # drop the stale paramiko client; next execute will reconnect
    r = com.execute(["test", "-f", sentinel])
    assert r.ok, f"sentinel didn't persist across reboot: {r}"

    snaps = driver.list_snapshots(vm_be)
    assert "pre-write" in snaps, f"snapshot missing from list: {snaps!r}"

    driver.shutdown_vm(vm_be, timeout=60.0)
    driver.restore_snapshot(vm_be, "pre-write")
    driver.start_vm(vm_be)
    com.close()
    r = com.execute(["test", "-f", sentinel])
    assert not r.ok, f"sentinel should be gone after restore: stdout={r.stdout!r}"


TESTS = [nginx_is_installed, hostname_matches, snapshot_lifecycle]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
