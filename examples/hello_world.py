"""hello_world: one VM, cloud-init bootstraps SSH + nginx, smoke-test it.

Portable plan — it declares topology only and pins no backend. Supply the
backend at run time with a connection profile:

    testrange describe examples/hello_world.py
    testrange graph examples/hello_world.py --order
    testrange run examples/hello_world.py --profile mybackend

``--profile mybackend`` reads the ``[mybackend]`` profile from ``./connect.toml``
(use ``--profile other.toml:mybackend`` for a different file).
See examples/connect.toml.example for the profile shape, and
docs/user/connecting-to-a-backend.md for the full workflow. Prerequisites:

    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar
"""

from __future__ import annotations

import sys

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.utils import SSHKey

_KEY = SSHKey.generate(comment="testrange-hello")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

pool1 = hyp.add_pool(StoragePool("pool1", 32))

hyp.add_switch(
    Switch(
        "switch1",
        Network("netA"),
        Network("netB"),
        cidr="172.31.0.0/24",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True),
    )
)
netA = hyp.networks["netA"]

hyp.vm(
    "web",
    cpu=CPU(2),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(netA, StaticAddr("172.31.0.150"))],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[
            PosixCred("root", password="root"),
            PosixCred("myuser", password="mypass", ssh_key=_KEY, admin=True),
        ],
        packages=[Apt("nginx")],
        post_install_commands=("echo hi > /tmp/hi",),
    ),
    communicator=SSHCommunicator("myuser"),
)

PLAN = Plan("hello-world", hyp)


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

    driver.shutdown_vm(vm_be, timeout=60.0)
    driver.start_vm(vm_be)
    com.close()
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
