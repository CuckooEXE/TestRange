"""One-shot script: spin up a VM, run a command, tear down.

Most examples wrap the orchestrator in a ``Test`` and run it through
``run_tests()``.  That's the right shape for test suites — but the
orchestrator is just a context manager, so you can use it directly
in a script when you don't need test reporting.

Run with::

    python examples/imperative_exec.py

If you want the VMs to **survive** the script (provisioning only —
no teardown on exit) call :meth:`Orchestrator.leak` before the
``with`` block ends::

    with Orchestrator(networks=[net], vms=[vm]) as orch:
        orch.vms["box"].exec(["apt-get", "install", "-y", "my-tool"]).check()
        orch.leak()
    # ``box`` is still running.  The teardown log lists the virsh
    # commands you'd run later to destroy it manually.

See :meth:`Orchestrator.leak` for the full contract (disk retention,
install-subnet pool pressure, memory accounting).
"""

from __future__ import annotations

from testrange import (
    VM,
    Credential,
    HardDrive,
    Memory,
    Orchestrator,
    VirtualNetwork,
    vNIC,
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
            vNIC("Net"),
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
