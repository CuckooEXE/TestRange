"""Remote libvirt orchestrator — drive a libvirtd on another host.

Before Phase A's storage-backend work, ``Orchestrator(host="qemu+ssh://…")``
silently broke the moment a qcow2 wasn't already sitting on the remote
host's disk — libvirt would get a control-plane call to define a domain
referencing a path that didn't exist on its side.

Now every disk operation routes through a :class:`StorageBackend` that
matches the libvirt URI: ``qemu:///system`` → local filesystem,
``qemu+ssh://[user@]host[:port]/system`` → SFTP + remote ``qemu-img``.
Nothing else in the user-visible API changes.

Prerequisites
-------------

- Passwordless SSH from the outer host to the target.  Paramiko uses
  standard discovery (``~/.ssh/config``, ssh-agent, default key files).
- ``libvirt-daemon-system`` + ``qemu-kvm`` + ``qemu-utils`` installed
  on the target.  The SSH user must be in the target's ``libvirt``
  group or run as root.
- Enough free disk on ``/var/tmp/testrange/<ssh_user>/`` on the target
  for cached images + the install-phase snapshot.

Run with::

    testrange run examples/remote_libvirt.py:gen_tests

Point ``REMOTE_HOST`` at your actual libvirt box.
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
    VirtualNetworkRef,
    run_tests,
    vCPU,
)

# Hostname or full qemu+ssh URI of the target libvirtd.
#  - Bare hostname: outer orchestrator auto-builds
#    ``qemu+ssh://<$USER>@<host>/system``.
#  - Full URI: pass through unchanged — useful for non-default ports,
#    explicit users, etc.
REMOTE_HOST = "kvm.internal.example.com"


def smoke(orch: Orchestrator) -> None:
    """Everything about this test function is backend-agnostic: the
    exact same body works against ``host="localhost"``.  Storage
    shipping is invisible at the user-facing layer."""
    vm = orch.vms["web"]

    # Cached install hits on the remote host's cache — the second run
    # against the same remote skips the install phase.
    result = vm.exec(["hostname"])
    result.check()
    assert result.stdout_text.strip() == "web"

    # The NAT network lives on the remote host too; outbound traffic
    # from the VM works as long as the remote libvirtd has internet.
    vm.exec(["curl", "-fsSI", "https://example.com"]).check()


def gen_tests() -> list[Test]:
    return [
        Test(
            Orchestrator(
                host=REMOTE_HOST,
                networks=[
                    VirtualNetwork(
                        "Net", "10.0.0.0/24", internet=True, dhcp=True,
                    ),
                ],
                vms=[
                    VM(
                        name="web",
                        iso=(
                            "https://cloud.debian.org/images/cloud/bookworm/"
                            "latest/debian-12-generic-amd64.qcow2"
                        ),
                        users=[Credential("root", "testrange")],
                        pkgs=[Apt("curl"), Apt("nginx")],
                        post_install_cmds=[
                            "systemctl enable --now nginx",
                        ],
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            VirtualNetworkRef("Net", ip="10.0.0.5"),
                        ],
                    ),
                ],
            ),
            smoke,
            name="remote-libvirt",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
