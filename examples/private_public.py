"""private_public: airgap vs internet, with a dual-homed client and reachability checks.

Prerequisites:
    testrange cache add https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2 \
        --name debian-13
    sudo tools/build-sidecar-image/build.sh
    testrange cache add tools/build-sidecar-image/testrange-sidecar.qcow2 --name testrange-sidecar

Usage:
    testrange describe examples/private_public.py
    testrange run examples/private_public.py
"""

from __future__ import annotations

import sys

from testrange import OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import (
    CPU,
    Memory,
    OSDrive,
    StoragePool,
)
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.drivers.proxmox import ProxmoxHypervisor
from testrange.networks import ManagedBuildSwitch, Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.utils import SSHKey
from testrange.vms import VMRecipe, VMSpec

_KEY = SSHKey.generate(comment="testrange-private-public")

_PRIVATE_WEB_IP = "10.20.0.100"
_CLIENT_PRIVATE_IP = "10.20.0.101"


PLAN = Plan(
    "private-public",
    ProxmoxHypervisor(
        host="40.160.34.83",
        password="Target123!",
        build_switch=ManagedBuildSwitch(uplink="vmbr0"),
        networks=[
            Switch(
                "priv-sw",
                Network("private-net"),
                cidr="10.20.0.0/24",
                # mgmt=True,  # gated pending ADR-0009 (mgmt switch semantics)
            ),
            Switch(
                "pub-sw",
                Network("public-net"),
                cidr="10.30.0.0/24",
                uplink="vmbr9",
                # mgmt=True,  # gated pending ADR-0009 (mgmt switch semantics)
                sidecar=Sidecar(
                    dhcp=True,
                    dns=True,
                    nat=True,
                    addr=StaticAddr("10.10.10.3/24", gw="10.10.10.1", dns=("1.1.1.1",)),
                ),
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="private-web",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        NetworkIface("private-net", addr=StaticAddr(_PRIVATE_WEB_IP)),
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
                            admin=True,
                        ),
                    ],
                    packages=[Apt("nginx")],
                    post_install_commands=(
                        "sh -c 'echo air-gapped > /var/www/html/index.html'",
                        "systemctl enable --now nginx",
                    ),
                ),
                communicator=SSHCommunicator("myuser"),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="public-web",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        NetworkIface("public-net", addr=DHCPAddr()),
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
                            admin=True,
                        ),
                    ],
                    packages=[Apt("nginx")],
                    post_install_commands=(
                        "sh -c 'echo internet-connected > /var/www/html/index.html'",
                        "systemctl enable --now nginx",
                    ),
                ),
                communicator=SSHCommunicator("myuser"),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="client",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 8),
                        NetworkIface("private-net", addr=StaticAddr(_CLIENT_PRIVATE_IP)),
                        NetworkIface("public-net", addr=DHCPAddr()),
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
                            admin=True,
                        ),
                    ],
                    packages=[Apt("curl")],
                ),
                communicator=SSHCommunicator("myuser"),
            ),
        ],
    ),
)


def client_can_curl_private_web(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_PRIVATE_WEB_IP}/"],
        timeout=20.0,
    )
    assert r.ok, f"curl to private-web failed: {r}"
    assert b"air-gapped" in r.stdout, f"unexpected body: {r.stdout!r}"


def client_can_curl_public_web(orch: OrchestratorHandle) -> None:
    public_ip = orch.vms["public-web"].communicator.host
    assert public_ip, "public-web has no discovered host"
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{public_ip}/"],
        timeout=20.0,
    )
    assert r.ok, f"curl to public-web failed: {r}"
    assert b"internet-connected" in r.stdout, f"unexpected body: {r.stdout!r}"


def private_web_cannot_reach_public_web(orch: OrchestratorHandle) -> None:
    public_ip = orch.vms["public-web"].communicator.host
    assert public_ip, "public-web has no discovered host"
    r = orch.vms["private-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", f"http://{public_ip}/"],
        timeout=15.0,
    )
    assert not r.ok, f"private-web reached public-web (should be isolated): {r.stdout!r}"


def public_web_cannot_reach_private_web(orch: OrchestratorHandle) -> None:
    r = orch.vms["public-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", f"http://{_PRIVATE_WEB_IP}/"],
        timeout=15.0,
    )
    assert not r.ok, f"public-web reached private-web (should be isolated): {r.stdout!r}"


def private_web_cannot_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["private-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", "-o", "/dev/null", "https://google.com/"],
        timeout=15.0,
    )
    assert not r.ok, "private-web reached the internet (should be air-gapped)"


def public_web_can_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["public-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://google.com/"],
        timeout=20.0,
    )
    assert r.ok, f"public-web failed to reach the internet: {r}"


TESTS = [
    client_can_curl_private_web,
    client_can_curl_public_web,
    private_web_cannot_reach_public_web,
    public_web_cannot_reach_private_web,
    private_web_cannot_reach_internet,
    public_web_can_reach_internet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
