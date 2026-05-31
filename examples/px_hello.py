"""Proxmox live smoke (PVE-9): build debian+nginx, reach it over QGA, assert.

This is the **scheme-pinned** example: it uses :class:`ProxmoxHypervisor` to
assert *this topology MUST run on Proxmox VE* (CORE-19), but the plan still
carries no connection — host/password/node/etc. come from a ``--profile``
(``examples/connect.toml.example``) at run time. For a fully backend-agnostic
plan that pins no scheme, see ``examples/hello_world.py`` (ADR-0015).

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar
    cp examples/connect.toml.example my-pve.toml   # then edit host/password + [pve.uplinks]
Usage:
    testrange describe examples/px_hello.py --profile my-pve.toml:pve
    testrange --log-level debug run examples/px_hello.py --profile my-pve.toml:pve

The VM is reached over the QEMU guest agent, so the orchestrator host needs no
route to it.

Build egress is the plan's ``build_switch`` (ADR-0016): an ordinary NAT
``Switch`` that routes out the ``egress`` uplink. ``egress`` is a logical name
the profile's ``[uplinks]`` map resolves to a host bridge with out-of-band
internet (TestRange attaches; it does not manufacture egress). Without a
``build_switch`` the build network is isolated, so a build that needs apt/pip
must declare one.
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
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

PLAN = Plan(
    "pve-smoke",
    ProxmoxHypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
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
