"""Proxmox live smoke (PVE-9): build debian+nginx, reach it over QGA, assert.

This is the **pinned-Proxmox** example: it hard-codes `ProxmoxHypervisor` because
the test genuinely targets PVE. For a portable plan that pins no backend and
takes its connection from a `--connect` profile, see `examples/hello_world.py`
(ADR-0015).

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar
Usage:
    testrange describe examples/px_hello.py
    testrange --log-level debug run examples/px_hello.py

Only host + password are required; user defaults to root@pam, node auto-detects,
storage defaults to 'local'. The VM is reached over the QEMU guest agent, so the
orchestrator host needs no route to it.

Build egress is opt-in (ADR-0014): ``build_switch=ManagedBuildSwitch(uplink="vmbr0")``
has TestRange manufacture and fence the build network's internet egress (an SDN
SNAT segment on Proxmox) out the named host bridge — ``vmbr0``, the one carrying
the default gateway. No manual internal bridge or host-NAT setup. Without a
build_switch the build network is isolated, so a build that needs apt/pip must
declare one.
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "pve-smoke",
    ProxmoxHypervisor(
        host="40.160.34.83",
        password="Target123!",
        build_switch=ManagedBuildSwitch(uplink="vmbr0"),
        networks=[
            Switch(
                "switch1",
                Network("netA"),
                cidr="172.31.0.0/24",
                sidecar=Sidecar(dhcp=True, dns=True),
            )
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
)


def nginx_installed(o: OrchestratorHandle) -> None:
    assert o.vms["web"].communicator.execute(["dpkg", "-l", "nginx"]).exit_code == 0


TESTS = [nginx_installed]

if __name__ == "__main__":
    sys.exit(0 if all(r.passed for r in run_tests(TESTS, PLAN)) else 1)
