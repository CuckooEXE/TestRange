"""nested_lab: a nested hypervisor running a few VMs inside it.

One L0 guest is a full libvirt/KVM host (``GuestHypervisor.libvirt``) carrying its
own inner plan — an isolated switch with DHCP/DNS and two inner VMs (``app`` and
``db``). The outer run phase brings the host up, then recurses into its inner plan
and brings the inner VMs up *inside* it (single-level nesting, ADR-0021). Test
code reaches the host through ``orch.vms`` and the inner VMs through
``orch.nested["lab"].vms``.

The inner VM disks build on the L0, so the inner plan declares its own NAT
``build_switch`` for apt egress during that build; the nested host then only boots
the pre-built disks, so the inner *run* switch itself needs no uplink.

Portable outer plan — bind the L0 backend at run time. Nesting needs a host that
exposes hardware virtualization to its guest (``CPU(nested=True)``):

    testrange describe examples/nested_lab.py
    testrange run --profile libvirt-local examples/nested_lab.py

Prerequisites:

    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.utils import SSHKey
from testrange.vms import GuestHypervisor, VMRecipe, VMSpec

_ADMIN = PosixCred("admin", ssh_key=SSHKey.generate(comment="testrange-nested"), admin=True)


def _inner_vm(name: str, *packages: str, post: tuple[str, ...] = ()) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(1024),
                OSDrive("inner-pool", 8),
                NetworkIface("inner-net", addr=DHCPAddr()),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            packages=[Apt("qemu-guest-agent"), *(Apt(p) for p in packages)],
            post_install_commands=("systemctl enable --now qemu-guest-agent", *post),
        ),
        communicator=NativeCommunicator(),
    )


PLAN = Plan(
    "nested-lab",
    Hypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "mgmt-sw",
                Network("mgmt-net"),
                cidr="10.61.0.0/24",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True),
            ),
        ],
        pools=[StoragePool("pool1", 48)],
        vms=[
            GuestHypervisor.libvirt(
                spec=VMSpec(
                    name="lab",
                    devices=[
                        CPU(4, nested=True),
                        Memory(8192),
                        OSDrive("pool1", 40),
                        NetworkIface("mgmt-net", addr=DHCPAddr()),
                    ],
                ),
                admin=_ADMIN,
                build_switch=Switch(
                    "innerbuild",
                    Network("innerbuild-net"),
                    cidr="10.98.99.0/24",
                    uplink="egress",
                    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
                ),
                networks=[
                    Switch(
                        "inner",
                        Network("inner-net"),
                        cidr="192.168.50.0/24",
                        sidecar=Sidecar(dhcp=True, dns=True),
                    ),
                ],
                pools=[StoragePool("inner-pool", 32)],
                vms=[
                    _inner_vm("app", "curl"),
                    _inner_vm(
                        "db",
                        "nginx",
                        post=(
                            "sh -c 'echo db-tier-online > /var/www/html/index.html'",
                            "systemctl enable --now nginx",
                        ),
                    ),
                ],
            ),
        ],
    ),
)


def nested_host_runs_libvirtd(orch: OrchestratorHandle) -> None:
    r = orch.vms["lab"].communicator.execute(["systemctl", "is-active", "libvirtd"])
    assert r.stdout.strip() == b"active", f"libvirtd not active on the nested host: {r}"


def inner_vms_are_up(orch: OrchestratorHandle) -> None:
    inner = orch.nested["lab"].vms
    for name in ("app", "db"):
        r = inner[name].communicator.execute(["hostname"])
        assert r.stdout.strip() == name.encode(), f"inner vm {name!r} not up: {r}"


def app_reaches_db_over_inner_network(orch: OrchestratorHandle) -> None:
    app = orch.nested["lab"].vms["app"].communicator
    r = app.execute(["curl", "-sf", "--max-time", "10", "http://db.inner-net/"], timeout=20.0)
    assert r.ok and b"db-tier-online" in r.stdout, f"app could not reach db inside the lab: {r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    nested_host_runs_libvirtd,
    inner_vms_are_up,
    app_reaches_db_over_inner_network,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
