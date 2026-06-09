"""multi_tier_app: a public web tier and an air-gapped backend tier.

Two guests across two switches. ``web`` is multi-homed — a DHCP NIC on the NAT'd
``edge`` switch (its default route + internet) and a static NIC on the isolated
``backend`` switch. ``db`` sits alone on ``backend``: no uplink, no sidecar, so it
has internal L2 to ``web`` but no path off-segment. ``db`` also carries a data
disk (a second ``HardDrive`` beside its OS drive). The tests assert the tier
boundary: ``web`` egresses, ``db`` does not, ``web`` reaches ``db`` internally, and
``db`` sees its extra disk.

Portable plan — bind a backend at run time:

    testrange describe examples/multi_tier_app.py
    testrange run --profile libvirt-local examples/multi_tier_app.py

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
from testrange.devices import CPU, HardDrive, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

_DB_IP = "10.40.0.101"


PLAN = Plan(
    "multi-tier-app",
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
                "edge",
                Network("edge-net"),
                cidr="10.30.0.0/24",
                uplink="egress",
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
            Switch("backend", Network("backend-net"), cidr="10.40.0.0/24"),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("edge-net", addr=DHCPAddr()),
                        NetworkIface("backend-net", addr=StaticAddr("10.40.0.100")),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    packages=[Apt("qemu-guest-agent"), Apt("curl")],
                    post_install_commands=("systemctl enable --now qemu-guest-agent",),
                ),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="db",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        HardDrive("pool1", 4),
                        NetworkIface("backend-net", addr=StaticAddr(_DB_IP)),
                    ],
                ),
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    packages=[Apt("qemu-guest-agent"), Apt("nginx")],
                    post_install_commands=(
                        "sh -c 'echo backend-tier-online > /var/www/html/index.html'",
                        "systemctl enable --now qemu-guest-agent nginx",
                    ),
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def web_egresses_through_nat(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://deb.debian.org/"],
        timeout=20.0,
    )
    assert r.ok, f"web tier could not reach the internet through NAT: {r}"


def db_is_air_gapped(orch: OrchestratorHandle) -> None:
    r = orch.vms["db"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", "-o", "/dev/null", "https://deb.debian.org/"],
        timeout=15.0,
    )
    assert not r.ok, "air-gapped backend tier reached the internet"


def web_reaches_db_internal_tier(orch: OrchestratorHandle) -> None:
    r = orch.vms["web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_DB_IP}/"], timeout=20.0
    )
    assert r.ok and b"backend-tier-online" in r.stdout, f"web could not reach the db tier: {r}"


def db_sees_its_data_disk(orch: OrchestratorHandle) -> None:
    r = orch.vms["db"].communicator.execute(["lsblk", "-dn", "-o", "TYPE"])
    disks = [ln for ln in r.stdout.decode().split() if ln == "disk"]
    assert len(disks) >= 2, f"db should see its OS drive + data disk, saw {r.stdout!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    web_egresses_through_nat,
    db_is_air_gapped,
    web_reaches_db_internal_tier,
    db_sees_its_data_disk,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
