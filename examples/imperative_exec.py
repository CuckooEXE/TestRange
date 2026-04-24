"""One-shot script: spin up a VM, run a command, tear down.

Most examples wrap the orchestrator in a ``Test`` and run it through
``run_tests()``.  That's the right shape for test suites — but the
orchestrator is just a context manager, so you can use it directly
in a script when you don't need test reporting.

Run with::

    python examples/imperative_exec.py
"""

from __future__ import annotations

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    VirtualNetwork,
    VirtualNetworkRef,
    vCPU,
)

DEBIAN_CLOUD = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/"
    "debian-12-generic-amd64.qcow2"
)


def main() -> None:
    net = VirtualNetwork("Net", "10.10.0.0/24", internet=True)

    vm = VM(
        name="box",
        iso=DEBIAN_CLOUD,
        users=[Credential("root", "testrange")],
        devices=[
            vCPU(1),
            Memory(1),
            HardDrive(10),
            VirtualNetworkRef("Net"),
        ],
    )

    # Entering the context manager provisions the network + VM and
    # waits for the guest agent.  Exiting destroys everything — even
    # if an exception escapes the block.
    with Orchestrator(networks=[net], vms=[vm]) as orch:
        box = orch.vms["box"]

        result = box.exec(["uname", "-a"])
        result.check()
        print(result.stdout_text.strip())


if __name__ == "__main__":
    main()
