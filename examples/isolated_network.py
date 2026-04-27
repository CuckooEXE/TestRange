"""Assert that an isolated network really is isolated.

Creates a VM on a ``VirtualNetwork`` with ``internet=False`` — the
backend installs no NAT forwarding rules for this bridge — and
verifies that outbound traffic has nowhere to go.  This is the
positive proof that ``internet=False`` works; other tests can rely
on it for security assertions (e.g. "our service should fail closed
when the internet is gone").

The guest agent still works because it's a virtio-serial channel,
not a TCP one — there's no network port involved in talking to the
VM even when its network is cut off.

Run with::

    testrange run examples/isolated_network.py:gen_tests
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


def assert_airgapped(orch: Orchestrator) -> None:
    vm = orch.vms["jail"]

    # DNS should still resolve against the backend's bridge-local
    # DNS service (dns=True) even though the bridge has no upstream path.
    resolved = vm.exec(["getent", "hosts", "jail.Airgap"])
    resolved.check()

    # But outbound HTTP/HTTPS has no route — curl should fail quickly.
    # Use --max-time so the assertion doesn't gate on a 30s DNS/TCP timeout.
    result = vm.exec(
        ["curl", "-sS", "--max-time", "5", "https://www.google.com/"],
        timeout=15,
    )
    assert result.exit_code != 0, (
        f"expected curl to the public internet to FAIL, but it returned "
        f"exit 0; isolation is broken. stdout: {result.stdout!r}"
    )

    # And guest agent still works — we just ran three commands on it.
    assert vm.hostname() == "jail"


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "Airgap",
                        "10.13.0.0/24",
                        internet=False,  # the whole point
                        dhcp=True,
                        dns=True,
                    ),
                ],
                vms=[
                    VM(
                        name="jail",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        pkgs=[Apt("curl")],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),  # 10 GiB OS disk
                            vNIC("Airgap"),
                        ],
                    ),
                ],
            ),
            assert_airgapped,
            name="isolated-network",
        ),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
