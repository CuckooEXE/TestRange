"""Nested hypervisor — a VM that runs its own inner VMs.

TestRange's :class:`~testrange.Hypervisor` is a VM that carries three
extra fields — ``orchestrator``, ``vms``, ``networks`` — describing an
*inner* layer it hosts.  The outer orchestrator:

1. Provisions the hypervisor VM normally (apt-installs libvirt,
   enables libvirtd, etc.).
2. Once the hypervisor's communicator is reachable, builds a fresh
   :class:`~testrange.LibvirtOrchestrator` pointed at
   ``qemu+ssh://<user>@<hypervisor-ip>/system`` via
   :meth:`~testrange.LibvirtOrchestrator.root_on_vm`.
3. Enters that inner orchestrator via :class:`ExitStack` so teardown
   unwinds from the top: inner VMs → inner networks → outer hypervisor
   VM → outer networks.

Prerequisites
-------------

- ``kvm_intel.nested=1`` (Intel) or ``kvm_amd.nested=1`` (AMD) on the
  physical host.  See :doc:`/usage/installation`.
- Key-based SSH from the outer host to ``root@<hypervisor-ip>`` —
  libvirt's ``qemu+ssh://`` does not do password auth.  The
  :class:`~testrange.Credential` carries the matching public key via
  ``ssh_key=``.

Cross-layer networking
----------------------

- Inner VMs can reach the outside world (and any sibling L1 VM on the
  outer network) — inner NAT lets outbound traffic through.
- Sibling L1 VMs **cannot** reach inner VMs directly.  Bridged-mode
  nesting is planned but not implemented in v1.

Run with::

    testrange run examples/nested_hypervisor.py:gen_tests
"""

from __future__ import annotations

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    Hypervisor,
    LibvirtOrchestrator,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    VirtualNetworkRef,
    run_tests,
    vCPU,
)

DEBIAN_CLOUD = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/"
    "debian-12-generic-amd64.qcow2"
)

# Public key of the keypair your ssh-agent holds — substituted into
# ``~root/.ssh/authorized_keys`` on the hypervisor VM by cloud-init so
# ``qemu+ssh://`` can log in.
with open(__import__("pathlib").Path("~/.ssh/id_ed25519.pub").expanduser()) as _f:
    SSH_PUBLIC_KEY = _f.read().strip()


def smoke(orch: Orchestrator) -> None:
    """The outer test function never sees the inner orchestrator —
    it's entered around the test body by the outer provisioning.
    We just exercise the outer VM (hypervisor) here to confirm the
    inner layer came up without failing teardown."""
    hv = orch.vms["hv"]

    # libvirtd is running inside the hypervisor VM.
    hv.exec(["systemctl", "is-active", "libvirtd"]).check()

    # The inner orchestrator has already provisioned ``inner-web``
    # by the time this runs — the inner VM is visible via virsh.
    result = hv.exec(["virsh", "-c", "qemu:///system", "list", "--all"])
    result.check()
    assert b"inner-web" in result.stdout or b"tr-inner-w" in result.stdout, (
        f"inner VM not seen by hypervisor's libvirt: {result.stdout_text!r}"
    )


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                networks=[
                    VirtualNetwork(
                        "OuterNet", "10.0.0.0/24",
                        internet=True, dhcp=True,
                    ),
                ],
                vms=[
                    Hypervisor(
                        name="hv",
                        iso=DEBIAN_CLOUD,
                        users=[
                            Credential(
                                "root", "testrange",
                                ssh_key=SSH_PUBLIC_KEY,
                            ),
                        ],
                        devices=[
                            vCPU(2),
                            # Enough headroom for inner VM memory +
                            # libvirtd + apt cache.  Tune upward for
                            # heavier inner workloads.
                            Memory(4),
                            # Inner qcow2 images live under
                            # ``/var/tmp/testrange/<user>/`` on this
                            # disk — size it for the sum of inner
                            # base images + overlays.
                            HardDrive(40),
                            VirtualNetworkRef("OuterNet", ip="10.0.0.10"),
                        ],
                        communicator="ssh",
                        orchestrator=LibvirtOrchestrator,
                        networks=[
                            VirtualNetwork(
                                "InnerNet", "10.42.0.0/24",
                                internet=True, dhcp=True,
                            ),
                        ],
                        vms=[
                            VM(
                                name="inner-web",
                                iso=DEBIAN_CLOUD,
                                users=[
                                    Credential(
                                        "root", "testrange",
                                        ssh_key=SSH_PUBLIC_KEY,
                                    ),
                                ],
                                pkgs=[Apt("nginx")],
                                post_install_cmds=[
                                    "systemctl enable --now nginx",
                                ],
                                devices=[
                                    vCPU(1),
                                    Memory(1),
                                    HardDrive(10),
                                    VirtualNetworkRef(
                                        "InnerNet", ip="10.42.0.5",
                                    ),
                                ],
                                communicator="ssh",
                            ),
                        ],
                    ),
                ],
            ),
            smoke,
            name="nested-hypervisor",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
