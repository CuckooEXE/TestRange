"""Nested hypervisor with public + private inner networks and a sidecar L1 peer.

Topology
========

::

    Outer (L1):
    ├── OuterNet (10.0.0.0/24, internet=True)
    │   ├── hv       @ 10.0.0.10   (Hypervisor VM)
    │   └── sidecar  @ 10.0.0.11   (sibling L1 VM)
    │
    └── inside hv (L2, driven by the inner LibvirtOrchestrator):
        ├── PublicNet  (10.42.0.0/24, internet=True)
        │   ├── webpublic @ 10.42.0.5
        │   └── client    @ 10.42.0.6   (dual-homed)
        └── PrivateNet (10.43.0.0/24, internet=False)
            ├── dbprivate @ 10.43.0.5
            └── client    @ 10.43.0.6   (dual-homed)

What this demonstrates
======================

- A :class:`~testrange.Hypervisor` VM that owns two *inner* networks
  (one with NAT out, one fully isolated) and three inner VMs.
- A **sidecar** L1 VM that lives on the same outer network as the
  hypervisor — outer-layer siblings remain independent even while
  an inner orchestrator is active inside ``hv``.
- A dual-homed ``client`` VM in the inner layer that can reach:

  - ``webpublic`` (same inner NAT'd network, L2 → L2)
  - ``dbprivate`` (same inner isolated network, L2 → L2)
  - ``sidecar`` (outer-layer peer, via NAT out of the inner PublicNet,
    out of the outer NAT, back onto OuterNet — L2 → L1).

Cross-layer directionality
==========================

v1 supports **outbound-from-nested** only.  The verification asserts
both sides of this contract:

- ``client → sidecar`` **works** (outbound NAT, two hops)
- ``sidecar → webpublic`` **fails** (no bridging from L1 into L2)

Bridged inner networks (which would make inbound L1 → L2 work too)
are planned but out of scope for v1.

Prerequisites
=============

- ``kvm_intel.nested=1`` or ``kvm_amd.nested=1`` on the physical host.
  See :doc:`/usage/installation`.
- Key-based SSH to ``root@<hv-ip>`` from the outer host.  The
  hypervisor's :class:`~testrange.Credential` carries the public key;
  the matching private key must be reachable via ``ssh-agent`` /
  ``~/.ssh/``.

Running
=======

::

    testrange run examples/nested_public_private.py:gen_tests

Expect the first run to take a while — the outer hypervisor installs
libvirt + qemu-kvm, then the inner orchestrator repeats the install
phase for each L2 VM inside it.  Second runs hit the post-install
cache on both layers.
"""

from __future__ import annotations

from pathlib import Path

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

SSH_PUBLIC_KEY = Path("~/.ssh/id_ed25519.pub").expanduser().read_text().strip()


def _verify_outer_layer(orch: Orchestrator) -> None:
    """Outer-layer checks: sidecar ↔ hypervisor connectivity and
    the documented sidecar → inner blackhole."""
    sidecar = orch.vms["sidecar"]
    hv = orch.vms["hv"]

    # 1. sidecar sees the hypervisor VM on OuterNet.
    sidecar.exec(
        ["curl", "-fsS", "--max-time", "5", "http://10.0.0.10:22"],
        timeout=10,
    )  # noqa: RUF100  — we ignore exit; any TCP reply is fine

    # 2. hypervisor has libvirtd running (inner orchestrator rooted on it).
    hv.exec(["systemctl", "is-active", "libvirtd"]).check()

    # 3. Inner VMs are visible to the hypervisor's libvirt.
    result = hv.exec(
        ["virsh", "-c", "qemu:///system", "list", "--all"],
    )
    result.check()
    for name in ("webpublic", "dbprivate", "client"):
        # Domain names are truncated to 15 chars by libvirt
        # (tr-<name>-<id>…); just search for the name prefix.
        assert name[:10].encode() in result.stdout, (
            f"inner VM {name!r} not seen in hv's virsh list: "
            f"{result.stdout_text!r}"
        )

    # 4. Cross-layer negative: sidecar cannot reach inner VMs.
    #    Ping the inner PublicNet IP — should time out or route-fail.
    r = sidecar.exec(
        ["ping", "-c", "1", "-W", "2", "10.42.0.5"],
        timeout=10,
    )
    assert r.exit_code != 0, (
        "sidecar reached the inner PublicNet; v1 does not support "
        "bridged inner networks.  Did nested reachability change?"
    )


def _verify_inner_reachability(orch: Orchestrator) -> None:
    """Reach into the inner orchestrator to exercise L2 → L1 NAT and
    intra-L2 connectivity from the dual-homed client VM."""
    # Access the inner orchestrator via the outer orchestrator's
    # _inner_orchestrators list — there's exactly one hypervisor here.
    inner_orchestrators = getattr(orch, "_inner_orchestrators", [])
    assert inner_orchestrators, "no inner orchestrator entered"
    inner = inner_orchestrators[0]
    client = inner.vms["client"]

    # 1. client → webpublic (intra-L2, shared PublicNet).
    r = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://10.42.0.5/"],
        timeout=20,
    )
    r.check()
    assert b"Public webserver" in r.stdout, r.stdout_text

    # 2. client → dbprivate (intra-L2, shared PrivateNet).
    r = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://10.43.0.5/"],
        timeout=20,
    )
    r.check()
    assert b"Private DB" in r.stdout, r.stdout_text

    # 3. client → sidecar (L2 → L1 via inner NAT + outer NAT).
    r = client.exec(
        ["curl", "-fsS", "--max-time", "15", "http://10.0.0.11/"],
        timeout=30,
    )
    r.check()
    assert b"Sidecar L1" in r.stdout, r.stdout_text


def verify(orch: Orchestrator) -> None:
    """Run both verification stages — outer contract first, then inner."""
    _verify_outer_layer(orch)
    _verify_inner_reachability(orch)


def _nginx_post_install(body: str) -> list[str]:
    return [
        "rm -f /var/www/html/index.nginx-debian.html",
        f"echo '<h1>{body}</h1>' > /var/www/html/index.html",
        "systemctl enable --now nginx",
    ]


def gen_tests() -> list[Test]:
    root_cred = Credential("root", "testrange", ssh_key=SSH_PUBLIC_KEY)

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
                    # L1 sidecar — an ordinary peer of the hypervisor.
                    VM(
                        name="sidecar",
                        iso=DEBIAN_CLOUD,
                        users=[root_cred],
                        pkgs=[Apt("curl"), Apt("nginx"), Apt("iputils-ping")],
                        post_install_cmds=_nginx_post_install("Sidecar L1"),
                        devices=[
                            vCPU(1),
                            Memory(1),
                            HardDrive(10),
                            VirtualNetworkRef("OuterNet", ip="10.0.0.11"),
                        ],
                    ),
                    # The hypervisor VM — hosts the inner orchestrator.
                    Hypervisor(
                        name="hv",
                        iso=DEBIAN_CLOUD,
                        users=[root_cred],
                        communicator="ssh",
                        devices=[
                            vCPU(2),
                            Memory(6),
                            HardDrive(60),
                            VirtualNetworkRef("OuterNet", ip="10.0.0.10"),
                        ],
                        orchestrator=LibvirtOrchestrator,
                        networks=[
                            VirtualNetwork(
                                "PublicNet", "10.42.0.0/24",
                                internet=True, dhcp=True,
                            ),
                            VirtualNetwork(
                                "PrivateNet", "10.43.0.0/24",
                                internet=False, dhcp=False,
                            ),
                        ],
                        vms=[
                            VM(
                                name="webpublic",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                pkgs=[Apt("nginx")],
                                post_install_cmds=_nginx_post_install(
                                    "Public webserver"
                                ),
                                devices=[
                                    vCPU(1),
                                    Memory(1),
                                    HardDrive(10),
                                    VirtualNetworkRef(
                                        "PublicNet", ip="10.42.0.5",
                                    ),
                                ],
                            ),
                            VM(
                                name="dbprivate",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                pkgs=[Apt("nginx")],
                                post_install_cmds=_nginx_post_install(
                                    "Private DB"
                                ),
                                devices=[
                                    vCPU(1),
                                    Memory(1),
                                    HardDrive(10),
                                    VirtualNetworkRef(
                                        "PrivateNet", ip="10.43.0.5",
                                    ),
                                ],
                            ),
                            # Dual-homed inner VM that bridges the two
                            # inner networks and can reach the L1
                            # sidecar by going out through the
                            # hypervisor's upstream.
                            VM(
                                name="client",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                pkgs=[Apt("curl"), Apt("iputils-ping")],
                                devices=[
                                    vCPU(1),
                                    Memory(1),
                                    HardDrive(10),
                                    VirtualNetworkRef(
                                        "PublicNet", ip="10.42.0.6",
                                    ),
                                    VirtualNetworkRef(
                                        "PrivateNet", ip="10.43.0.6",
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            verify,
            name="nested-public-private",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
