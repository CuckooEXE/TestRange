"""Proxmox live smoke (PVE-9): build debian+nginx, reach it over QGA, assert.

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

Build egress on a single-public-IP host: the host won't DHCP the sidecar's MAC,
so point ``build_uplink`` at an internal bridge the host NATs out its real NIC,
and give the sidecar a static ``build_uplink_addr`` on that bridge (NET-7).
Define the bridge the PVE-native way (in /etc/network/interfaces) so preflight's
bridge check sees it — a live ``ip link add`` bridge is invisible to PVE's API:

    cat >> /etc/network/interfaces <<'EOF'
    auto vmbr9
    iface vmbr9 inet static
        address 10.10.10.1/24
        bridge-ports none
        bridge-stp off
        bridge-fd 0
        post-up sysctl -w net.ipv4.ip_forward=1
        post-up iptables -t nat -A POSTROUTING -s 10.10.10.0/24 -o vmbr0 -j MASQUERADE
        post-up iptables -A FORWARD -i vmbr9 -o vmbr0 -j ACCEPT
        post-up iptables -A FORWARD -i vmbr0 -o vmbr9 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    EOF
    ifreload -a
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    ProxmoxHypervisor(
        host="40.160.34.83",
        password="Target123!",
        build_uplink="vmbr9",
        build_uplink_addr=StaticAddr("10.10.10.2/24", gw="10.10.10.1", dns=("1.1.1.1",)),
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
    name="pve-smoke",
)


def nginx_installed(o: OrchestratorHandle) -> None:
    assert o.vms["web"].communicator.execute(["dpkg", "-l", "nginx"]).exit_code == 0


TESTS = [nginx_installed]

if __name__ == "__main__":
    sys.exit(0 if all(r.passed for r in run_tests(TESTS, PLAN)) else 1)
