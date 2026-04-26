"""Cross-network DNS with the network name acting as TLD.

Two isolated networks (``Engineering`` and ``Ops``) and three VMs:

- ``auth`` lives on ``Engineering`` only.
- ``logs`` lives on ``Ops`` only.
- ``jump`` is dual-homed on both.

The jump host looks each peer up by *FQDN* — ``auth.Engineering`` and
``logs.Ops`` — and must get distinct answers.  TestRange deliberately
does *not* register bare ``auth`` / ``logs`` in DNS: every cross-VM
lookup is explicit about which network it belongs to.

Run with::

    testrange run examples/cross_network_dns.py:gen_tests
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


def check_dns(orch: Orchestrator) -> None:
    jump = orch.vms["jump"]

    eng_lookup = jump.exec(["getent", "hosts", "auth.Engineering"]).check()
    ops_lookup = jump.exec(["getent", "hosts", "logs.Ops"]).check()

    eng_ip = eng_lookup.stdout_text.split()[0]
    ops_ip = ops_lookup.stdout_text.split()[0]

    # Different networks → different subnets → different IPs.
    assert eng_ip.startswith("10.18.1."), eng_ip
    assert ops_ip.startswith("10.18.2."), ops_ip

    # And bare hostnames *don't* resolve across the bridges — this is
    # the "FQDN-only" invariant that makes cross-network names unambiguous.
    bare = jump.exec(["getent", "hosts", "auth"])
    assert bare.exit_code != 0, (
        f"bare 'auth' should NOT resolve across the bridge, "
        f"but getent returned: {bare.stdout_text!r}"
    )


def gen_tests() -> list[Test]:
    users = [Credential("root", "testrange")]
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Engineering", "10.18.1.0/24",
                                    internet=False, dhcp=True, dns=True),
                    VirtualNetwork("Ops", "10.18.2.0/24",
                                    internet=False, dhcp=True, dns=True),
                ],
                vms=[
                    VM(
                        name="auth",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Engineering"),
                        ],
                    ),
                    VM(
                        name="logs",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Ops"),
                        ],
                    ),
                    VM(
                        name="jump",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=users,
                        pkgs=[Apt("dnsutils")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            vNIC("Engineering"),
                            vNIC("Ops"),
                        ],
                    ),
                ],
            ),
            check_dns,
            name="cross-network-dns",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
