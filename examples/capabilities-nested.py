"""capabilities-nested: one nested hypervisor running an inner plan (ADR-0021).

A portable outer :class:`Hypervisor` hosts a single ``GuestHypervisor`` —
``host-a``, a libvirt host built by cloud-init — which runs its own inner plan of
one VM (``webapp``). Bind a backend at run time with ``--profile``; the inner
backend is libvirt, synthesized at run time from the running guest (no inner
``--profile``).

The inner VM's disk is built on the outer (L0) backend with real egress through
the inner build switch, then booted on ``host-a`` at run. The inner run network
is isolated (sidecar DHCP/DNS, no uplink): inner-VM runtime egress is a separate
capability (NET-17) and is not exercised here.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange describe examples/capabilities-nested.py
    testrange run --profile <name> examples/capabilities-nested.py

The profile must map the ``egress`` uplink to a host bridge with out-of-band
internet egress, and the L0 host must have nested KVM enabled
(``/sys/module/kvm_intel/parameters/nested`` == Y).
"""

from __future__ import annotations

import sys

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

_KEY = SSHKey.generate(comment="capabilities-nested")
_ADMIN = PosixCred("admin", ssh_key=_KEY, admin=True)

_INNER_BUILD = Switch(
    "inner-build",
    Network("inner-build-net"),
    cidr="10.97.50.0/24",
    uplink="egress",
    sidecar=Sidecar(dhcp=True, dns=True, nat=True),
)

_WEBAPP = VMRecipe(
    spec=VMSpec(
        name="webapp",
        devices=[
            CPU(1),
            Memory(1024),
            OSDrive("inner-pool", 8),
            NetworkIface("inner-net", addr=DHCPAddr()),
        ],
    ),
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        packages=[Apt("qemu-guest-agent"), Apt("nginx")],
        post_install_commands=(
            "systemctl enable --now qemu-guest-agent nginx",
            "sh -c 'echo nested-hello > /var/www/html/index.html'",
        ),
    ),
    communicator=NativeCommunicator(),
)

PLAN = Plan(
    "capabilities-nested",
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
                "lab",
                Network("lab-net"),
                cidr="10.50.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("pool1", 128)],
        vms=[
            GuestHypervisor.libvirt(
                spec=VMSpec(
                    name="host-a",
                    devices=[
                        CPU(4, nested=True),
                        Memory(4096),
                        OSDrive("pool1", 30),
                        NetworkIface("lab-net", addr=DHCPAddr()),
                    ],
                ),
                admin=_ADMIN,
                build_switch=_INNER_BUILD,
                networks=[
                    Switch(
                        "inner",
                        Network("inner-net"),
                        cidr="192.168.50.0/24",
                        sidecar=Sidecar(dhcp=True, dns=True),
                    ),
                ],
                pools=[StoragePool("inner-pool", 32)],
                vms=[_WEBAPP],
            ),
        ],
    ),
)


def host_runs_libvirtd(orch: OrchestratorHandle) -> None:
    r = orch.nested["host-a"].host.communicator.execute(["virsh", "-c", "qemu:///system", "list"])
    assert r.ok, f"libvirtd not serving on the nested host: {r}"


def inner_webapp_reachable(orch: OrchestratorHandle) -> None:
    r = orch.nested["host-a"].vms["webapp"].communicator.execute(["true"])
    assert r.ok, f"inner VM native agent unreachable: {r}"


def inner_webapp_serves(orch: OrchestratorHandle) -> None:
    r = (
        orch.nested["host-a"]
        .vms["webapp"]
        .communicator.execute(
            ["curl", "-sf", "--max-time", "10", "http://localhost/"], timeout=20.0
        )
    )
    assert r.ok and b"nested-hello" in r.stdout, f"inner webapp not serving: {r}"


def inner_webapp_on_inner_subnet(orch: OrchestratorHandle) -> None:
    out = (
        orch.nested["host-a"]
        .vms["webapp"]
        .communicator.execute(["ip", "-o", "-4", "addr"])
        .stdout.decode()
    )
    assert "192.168.50." in out, f"inner VM not on the inner subnet: {out!r}"


TESTS = [
    host_runs_libvirtd,
    inner_webapp_reachable,
    inner_webapp_serves,
    inner_webapp_on_inner_subnet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
