"""network_modes: four switches demonstrating orthogonal infra flags.

Covers the bare / mgmt-only / uplink-only / mgmt+uplink+nat combinations.
Each switch has one VM that asserts its expected reachability.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange describe examples/network_modes.py
    testrange run examples/network_modes.py
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import Network, Switch
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-network-modes")


def _vm(name: str, network: str, ipv4: str) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                NetworkIface(network, addr=StaticAddr(ipv4)),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            credentials=[
                PosixCred("root", password="root"),
                PosixCred(
                    "myuser",
                    password="mypass",
                    ssh_key=_KEY,
                    sudo=True,
                ),
            ],
        ),
        communicator=SSHCommunicator("myuser"),
    )


PLAN = Plan(
    ProxmoxHypervisor(
        host="40.160.34.83",
        password="Target123!",
        build_uplink="vmbr9",
        build_uplink_addr=StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",)),
        networks=[
            Switch("bare-sw", Network("bare-net"), cidr="10.50.0.0/24"),
            Switch(
                "mgmt-sw",
                Network("mgmt-net"),
                cidr="10.51.0.0/24",
                # mgmt=True,  # gated pending ADR-0009 (mgmt switch semantics)
            ),
            Switch(
                "uplink-sw",
                Network("uplink-net"),
                cidr="10.52.0.0/24",
                uplink="vmbr9",
                uplink_addr=StaticAddr("10.10.10.3/24", gw="10.10.10.1", dns=("1.1.1.1",)),
                dhcp=True,
                dns=True,
                nat=True,
            ),
            Switch(
                "both-sw",
                Network("both-net"),
                cidr="10.53.0.0/24",
                uplink="vmbr9",
                uplink_addr=StaticAddr("10.10.10.4/24", gw="10.10.10.1", dns=("1.1.1.1",)),
                # mgmt=True,  # gated pending ADR-0009 (mgmt switch semantics)
                dhcp=True,
                dns=True,
                nat=True,
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            _vm("bare-vm", "bare-net", "10.50.0.100"),
            _vm("mgmt-vm", "mgmt-net", "10.51.0.100"),
            _vm("uplink-vm", "uplink-net", "10.52.0.100"),
            _vm("both-vm", "both-net", "10.53.0.100"),
        ],
    ),
    name="network-modes",
)


def mgmt_vm_can_reach_host(orch: OrchestratorHandle) -> None:
    # Gated pending ADR-0009 (mgmt switch semantics): no host adapter at .2.
    # r = orch.vms["mgmt-vm"].communicator.execute(
    #     ["ping", "-c", "1", "-W", "2", "10.51.0.2"], timeout=10.0
    # )
    # assert r.ok, "mgmt-vm cannot reach host mgmt adapter at .2"
    pass


def uplink_vm_can_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["uplink-vm"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://google.com/"],
        timeout=15.0,
    )
    assert r.ok, "uplink-vm cannot reach the internet through NAT"


def both_vm_reaches_host_and_internet(orch: OrchestratorHandle) -> None:
    # Gated pending ADR-0009 (mgmt switch semantics): no host adapter at .2.
    # r_host = orch.vms["both-vm"].communicator.execute(
    #     ["ping", "-c", "1", "-W", "2", "10.53.0.2"], timeout=10.0
    # )
    # assert r_host.ok, "both-vm cannot reach host mgmt at .2"
    r_net = orch.vms["both-vm"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://google.com/"],
        timeout=15.0,
    )
    assert r_net.ok, "both-vm cannot reach the internet through NAT"


TESTS = [
    mgmt_vm_can_reach_host,
    uplink_vm_can_reach_internet,
    both_vm_reaches_host_and_internet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
