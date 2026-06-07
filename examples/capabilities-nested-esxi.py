"""capabilities-nested-esxi: a nested ESXi hypervisor running an inner plan (ADR-0021).

A portable outer :class:`Hypervisor` hosts a single ``GuestHypervisor`` —
``esxi-a``, an ESXi node installed unattended via kickstart on the libvirt L0 —
which runs its own inner plan of one VM (``webapp``) over pyVmomi. Bind the outer
backend at run time with ``--profile``; the inner backend is ESXi, synthesized at
run time from the running guest (no inner ``--profile``).

The inner VM's disk is built on the outer (L0) backend with real egress through
the inner build switch (BUILD-14), then booted (qcow2→vmdk) on ``esxi-a`` at run.
This is what unblocks ESXi certification: the build VM gets its ``apt`` egress on
L0, and the nested ESXi only ever boots a pre-built disk — no VM-egress path is
needed on the ESXi node itself.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    testrange cache add ~/Desktop/VMware-VMvisor-Installer-8.0U3b-24280767.x86_64.iso \
        --name esxi-installer
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar
    export TESTRANGE_ESXI_LICENSE=XXXXX-XXXXX-XXXXX-XXXXX-XXXXX   # applied at install

Usage:
    testrange describe examples/capabilities-nested-esxi.py
    testrange run --profile <name> examples/capabilities-nested-esxi.py

The profile must map the ``egress`` uplink to a host bridge with out-of-band
internet egress, and the L0 host must have nested KVM enabled
(``/sys/module/kvm_intel/parameters/nested`` == Y) so the ESXi guest can power on
its own VMs. The license is read from ``TESTRANGE_ESXI_LICENSE`` (unset → the
install stays on the read-only evaluation edition, which cannot certify writes).
"""

from __future__ import annotations

import os
import sys

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.disk.libvirt import LibvirtOSDrive
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.utils import EcdsaKey, SSHKey
from testrange.vms import GuestHypervisor, VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="capabilities-nested-esxi")
_ESXI_KEY = EcdsaKey.generate(comment="capabilities-nested-esxi-host")
_ROOT = PosixCred("root", password="TestRangeEsxi2026!", ssh_key=_ESXI_KEY)
_ADMIN = PosixCred("admin", ssh_key=_KEY, admin=True)
_LICENSE = os.environ.get("TESTRANGE_ESXI_LICENSE") or None


PLAN = Plan(
    "capabilities-nested-esxi",
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
            GuestHypervisor.esxi(
                spec=VMSpec(
                    name="esxi-a",
                    firmware="bios",
                    devices=[
                        CPU(4, nested=True),
                        Memory(8192),
                        LibvirtOSDrive("pool1", 48, bus="sata"),
                        LibvirtNetworkIface("lab-net", model="e1000e", addr=DHCPAddr()),
                    ],
                ),
                root=_ROOT,
                installer_iso=CacheEntry("esxi-installer"),
                license=_LICENSE,
                build_switch=Switch(
                    "inner-build",
                    Network("inner-build-net"),
                    cidr="10.97.50.0/24",
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
                    VMRecipe(
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
                            credentials=[_ADMIN],
                            packages=[Apt("open-vm-tools"), Apt("nginx")],
                            post_install_commands=(
                                "systemctl enable --now open-vm-tools nginx",
                                "sh -c 'echo nested-esxi-hello > /var/www/html/index.html'",
                            ),
                        ),
                        communicator=NativeCommunicator(),
                    )
                ],
            ),
        ],
    ),
)


def esxi_host_answers_api(orch: OrchestratorHandle) -> None:
    r = orch.nested["esxi-a"].host.communicator.execute(["esxcli", "system", "version", "get"])
    assert r.ok, f"nested ESXi host not answering over SSH: {r}"


def inner_webapp_reachable(orch: OrchestratorHandle) -> None:
    r = orch.nested["esxi-a"].vms["webapp"].communicator.execute(["true"])
    assert r.ok, f"inner VM native agent (VMware Tools) unreachable: {r}"


def inner_webapp_serves(orch: OrchestratorHandle) -> None:
    r = (
        orch.nested["esxi-a"]
        .vms["webapp"]
        .communicator.execute(
            ["curl", "-sf", "--max-time", "10", "http://localhost/"], timeout=20.0
        )
    )
    assert r.ok and b"nested-esxi-hello" in r.stdout, f"inner webapp not serving: {r}"


def inner_webapp_on_inner_subnet(orch: OrchestratorHandle) -> None:
    out = (
        orch.nested["esxi-a"]
        .vms["webapp"]
        .communicator.execute(["ip", "-o", "-4", "addr"])
        .stdout.decode()
    )
    assert "192.168.50." in out, f"inner VM not on the inner subnet: {out!r}"


TESTS = [
    esxi_host_answers_api,
    inner_webapp_reachable,
    inner_webapp_serves,
    inner_webapp_on_inner_subnet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
