"""One test that provisions three different Linux distributions.

Hands ``iso=`` an upstream ``https://`` URL for each of Debian 12,
Ubuntu 24.04, and Rocky 9.  The base-image cache is keyed per-URL, so
the first run downloads and installs each distro once; subsequent
runs are near-instant.

Each VM is asked to identify itself via ``/etc/os-release``, which
every cloud image ships.

Run with::

    testrange run examples/multi_distro.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)


def identify(orch: Orchestrator) -> None:
    def distro_id(vm: VM) -> str:
        text = vm.read_text("/etc/os-release")
        for line in text.splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
        raise AssertionError(f"no ID= line in {vm.name}'s os-release")

    assert distro_id(orch.vms["deb"]) == "debian"
    assert distro_id(orch.vms["ubuntu"]) == "ubuntu"
    assert distro_id(orch.vms["rocky"]) == "rocky"


def gen_tests() -> list[Test]:
    users = [Credential("root", "testrange")]
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.19.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="deb",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        devices=[
                            vCPU(1), Memory(1), HardDrive(10),
                            vNIC("Net"),
                        ],
                    ),
                    VM(
                        name="ubuntu",
                        iso=(
                            "https://cloud-images.ubuntu.com/noble/current/"
                            "noble-server-cloudimg-amd64.img"
                        ),
                        users=users,
                        devices=[
                            vCPU(1), Memory(1), HardDrive(10),
                            vNIC("Net"),
                        ],
                    ),
                    VM(
                        name="rocky",
                        iso=(
                            "https://download.rockylinux.org/pub/rocky/9/"
                            "images/x86_64/"
                            "Rocky-9-GenericCloud.latest.x86_64.qcow2"
                        ),
                        users=users,
                        devices=[
                            vCPU(1), Memory(1), HardDrive(15),  # Rocky needs a bit more
                            vNIC("Net"),
                        ],
                    ),
                ],
            ),
            identify,
            name="multi-distro",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
