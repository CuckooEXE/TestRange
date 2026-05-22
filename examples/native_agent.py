"""native_agent: one VM reached over the hypervisor's native guest agent, not SSH.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange describe examples/native_agent.py
    testrange run examples/native_agent.py
"""

from __future__ import annotations

import os
import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.drivers.mock import MockHypervisor
from testrange.networks import Network, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

UPLINK = os.environ.get("TESTRANGE_UPLINK", "eth0")

PLAN = Plan(
    MockHypervisor(
        build_uplink=UPLINK,
        networks=[
            Switch(
                "switch1",
                Network("netA"),
                cidr="172.31.0.0/24",
                uplink=UPLINK,
                dhcp=True,
                dns=True,
                nat=True,
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
                        NetworkIface("netA", addr=DHCPAddr()),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    packages=[Apt("nginx"), Apt("qemu-guest-agent")],
                    post_install_commands=("systemctl enable --now qemu-guest-agent",),
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
    name="qga-demo",
)


def nginx_is_installed(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["dpkg", "-l", "nginx"])
    assert r.exit_code == 0, "nginx missing"


def hostname_matches(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(["hostname"])
    assert r.stdout.strip() == b"web", r


def write_then_read_roundtrips(orch: OrchestratorHandle) -> None:
    com = orch.vms["web"].communicator
    com.write_file("/root/marker.txt", b"qga-was-here\n")
    assert com.read_file("/root/marker.txt") == b"qga-was-here\n"


TESTS = [nginx_is_installed, hostname_matches, write_then_read_roundtrips]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
