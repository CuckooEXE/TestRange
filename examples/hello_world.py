"""Smallest possible TestRange example: one VM, one assertion.

Spins up a Debian VM on a NAT network, runs ``uname -r`` over the
guest-agent channel, and asserts the kernel string looks like a
Linux kernel version.  Cache-hits on the second run so the whole
thing takes a few seconds.

Run with::

    testrange run examples/hello_world.py:gen_tests
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


def smoke(orch: Orchestrator) -> None:
    vm = orch.vms["hello"]

    # exec() runs a command via the configured communicator (default
    # guest-agent on Linux) and returns a (exit_code, stdout, stderr)
    # NamedTuple.
    result = vm.exec(["uname", "-r"])
    result.check()

    kernel = result.stdout_text.strip()
    assert kernel, "expected a non-empty kernel version"
    # Debian 12 kernels start with 6.x at the time of writing; keep
    # the assertion loose so this example ages well.
    assert kernel[0].isdigit(), f"kernel string didn't start with a digit: {kernel!r}"


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork("Net", "10.10.0.0/24", internet=True),
                ],
                vms=[
                    VM(
                        name="hello",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),  # 10 GiB OS disk
                            vNIC("Net"),
                        ],
                    ),
                ],
            ),
            smoke,
            name="hello-world",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
