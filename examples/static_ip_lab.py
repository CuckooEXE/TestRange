"""A DHCP-less lab: every VM gets an explicit static address.

``VirtualNetwork(dhcp=False, ...)`` disables the backend's
bridge-local DHCP server entirely.  In that mode every attached
NIC must come with an explicit ``ip=`` on its ``vNIC`` —
TestRange's orchestrator checks this at provisioning time and
raises if you miss one.

Good fit for:

- Air-gapped labs where dynamic addressing isn't allowed
- Migration-ish scenarios where the addresses are part of the spec
- Tests that lean on ``/etc/hosts``-style static topology

Run with::

    testrange run examples/static_ip_lab.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Apt,
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


def check_lab(orch: Orchestrator) -> None:
    a = orch.vms["alpha"]
    b = orch.vms["bravo"]
    c = orch.vms["charlie"]

    # Each VM reports the address we handed it.
    assert "10.14.0.10" in a.exec(["ip", "-4", "addr"]).check().stdout_text
    assert "10.14.0.11" in b.exec(["ip", "-4", "addr"]).check().stdout_text
    assert "10.14.0.12" in c.exec(["ip", "-4", "addr"]).check().stdout_text

    # alpha → bravo → charlie ping mesh.  No DHCP involved.
    for src, dst_ip in [(a, "10.14.0.11"), (a, "10.14.0.12"),
                        (b, "10.14.0.10"), (b, "10.14.0.12"),
                        (c, "10.14.0.10"), (c, "10.14.0.11")]:
        r = src.exec(["ping", "-c", "1", "-W", "2", dst_ip])
        assert r.exit_code == 0, (
            f"{src.name} -> {dst_ip} ping FAILED: {r.stderr_text}"
        )


def gen_tests() -> list[Test]:
    users = [Credential("root", "testrange")]
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "Lab",
                        "10.14.0.0/24",
                        dhcp=False,       # every NIC must supply ip=
                        internet=False,   # keep the lab isolated
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="alpha",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        pkgs=[Apt("iputils-ping")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Lab", ip="10.14.0.10"),
                        ],
                    ),
                    VM(
                        name="bravo",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        pkgs=[Apt("iputils-ping")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Lab", ip="10.14.0.11"),
                        ],
                    ),
                    VM(
                        name="charlie",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        pkgs=[Apt("iputils-ping")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Lab", ip="10.14.0.12"),
                        ],
                    ),
                ],
            ),
            check_lab,
            name="static-ip-lab",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
