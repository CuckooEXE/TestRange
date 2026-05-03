"""Demonstrate ESXi-style mutual isolation between management and a VM segment.

When a VM lives on a network that the hypervisor itself has no IP
on, the hypervisor cannot route to the VM and vice versa.  This is
the "different vSwitches" pattern from ESXi, but expressible on
libvirt too: a network defined without an ``<ip>`` block on its
bridge has no host-side gateway, so the host has no L3 path to any
VM on the bridge — and the VMs have no path off it either, except
through a peer that happens to be dual-homed.

Topology
========

::

    test runner / libvirt host
    │
    ├── MgmtNet (10.0.0.0/24, host has 10.0.0.1, dhcp=True, internet=True)
    │   └── mgmtside     — Debian + nginx ("Mgmt-side server")
    │
    └── IsolatedNet (10.99.0.0/24, host_isolated=True — host has NO IP)
        ├── isolated_a @ 10.99.0.5  — Debian + nginx ("Isolated A")
        └── isolated_b @ 10.99.0.6  — Debian + curl

The runner sits on the libvirt host (we run ``qemu:///system``).  It
has the ``MgmtNet`` gateway IP on its bridge but nothing on the
``IsolatedNet`` bridge.  ``isolated_a`` / ``isolated_b`` are reached
via the QEMU guest agent — that channel rides virtio-serial
(host-mediated, no IP routing required), so we can ``exec`` inside
those VMs even though the runner can't curl them.

What this proves
================

1. **Runner can curl ``mgmtside``** (control case — proves routing
   works on the management network).
2. **Runner CANNOT curl ``isolated_a``** by IP — the host has no
   route to ``10.99.0.0/24`` because the bridge carries no host
   IP.  This is the load-bearing assertion.
3. **``isolated_b`` CAN curl ``isolated_a``** — proves the bridge
   itself works at L2/L3 between peers; the isolation is *only*
   from the host's perspective.
4. **``isolated_a`` CANNOT reach ``mgmtside``** — VMs on the
   isolated bridge have no path off it (no gateway, no router),
   so cross-network traffic black-holes.

Implication for :meth:`~testrange.proxy.base.Proxy`
===================================================

A test author who calls ``orch.proxy().forward(("10.99.0.5", 80))``
on an ``host_isolated`` network would see the forward bind a local
listener fine, but the *first* connection through it would fail
with ``open_channel`` "no route to host" or "channel administratively
prohibited" depending on the SSH server's response.  This is the
correct behaviour: the proxy reaches what the *hypervisor* can
reach, and ``host_isolated=True`` removes the hypervisor's L3 path
on purpose.  Tests that need runner-side reachability to such VMs
must either:

* drop the ``host_isolated`` flag (give the host an IP), or
* go through a *sidecar VM* on the isolated bridge (a peer on the
  same network the hypervisor IS routed to, which can act as a
  jump host via its guest-agent ``exec``).

This example uses the second pattern in step 3 — ``isolated_b`` is
the sidecar that proves intra-bridge connectivity.

Running
=======

::

    testrange run examples/host_isolated_network.py:gen_tests

Local libvirt only.  No SSH agent needed.  ~3 min cold cache,
seconds warm.
"""

from __future__ import annotations

import subprocess

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


DEBIAN_CLOUD = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/"
    "debian-12-generic-amd64.qcow2"
)


def _curl_from_runner(url: str, timeout: float = 6.0) -> tuple[int, str]:
    """Curl from the test-runner process itself (= the libvirt host
    in local-libvirt mode).

    Returns ``(exit_code, body)``.  Non-zero exit codes are the
    success criterion for the negative assertions; we don't
    ``check=True`` because failure is exactly what step 2 is
    proving.
    """
    r = subprocess.run(
        ["curl", "-fsS", "--max-time", str(int(timeout)), url],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout


def verify(orch: Orchestrator) -> None:
    """The four-assertion chain that proves ESXi-style mutual isolation
    holds between the runner / hypervisor and the isolated bridge.

    Order: positive cases first (so a routing regression on
    ``MgmtNet`` surfaces with a specific message), then the negative
    runner-to-isolated, then the cross-VM positive, then the negative
    isolated-to-mgmt.  Each assertion's failure message names what
    it was supposed to prove so a regression is immediately
    diagnosable.
    """
    isolated_a = orch.vms["isolated_a"]
    isolated_b = orch.vms["isolated_b"]

    # 1. Runner → mgmtside.  The runner has 10.0.0.1 on the MgmtNet
    # bridge, mgmtside is on 10.0.0.10 (pinned static IP — see
    # gen_tests).  This MUST work — if it doesn't, MgmtNet itself
    # is broken and the isolation assertions below would be a false
    # positive (you'd be proving the runner can't reach ANY VM, not
    # specifically that it can't reach the isolated ones).
    #
    # Uses the IP directly (not ``mgmtside.MgmtNet`` FQDN) because
    # the test runner's ``/etc/resolv.conf`` doesn't point at
    # libvirt's bridge dnsmasq by default — DNS resolution from
    # the host requires either a resolved-stub config we don't
    # touch, or NSS surgery.  IP form sidesteps the resolver
    # entirely.
    rc, body = _curl_from_runner("http://10.0.0.10/")
    assert rc == 0 and "Mgmt-side server" in body, (
        f"control case: runner couldn't reach mgmtside at 10.0.0.10 "
        f"(rc={rc}, body={body!r}).  MgmtNet routing is broken — "
        "the isolation assertions below cannot be trusted until "
        "this passes."
    )

    # 2. Runner → isolated_a.  This MUST FAIL.  The runner has no
    # IP on the IsolatedNet bridge (the network was defined with
    # host_isolated=True, no <ip> block in libvirt's network XML),
    # so the kernel has no route to 10.99.0.0/24 — every connect()
    # attempt yields "Network is unreachable".  curl exits non-zero
    # with the "Could not resolve host" or "couldn't connect"
    # message.
    rc, body = _curl_from_runner("http://10.99.0.5/")
    assert rc != 0, (
        f"isolation breach: runner reached 10.99.0.5 from the host "
        f"despite host_isolated=True (curl returned rc={rc}, "
        f"body={body!r}).  Either libvirt's network XML still has "
        "an <ip> block on the IsolatedNet bridge, or someone "
        "configured a route to 10.99.0.0/24 on the host outside of "
        "libvirt.  This is the load-bearing assertion of the "
        "example — its failure means the topology this whole "
        "example was built to demonstrate is broken."
    )

    # 3. isolated_b → isolated_a (intra-bridge curl).  Both VMs are
    # on the same Linux bridge; even with no host IP, L2 forwarding
    # between bridge ports still works.  This is the positive proof
    # that the isolation is *one-sided* — the bridge itself isn't
    # broken, the host is just not on it.
    r = isolated_b.exec(
        ["curl", "-fsS", "--max-time", "6", "http://10.99.0.5/"],
        timeout=15,
    )
    assert r.exit_code == 0 and b"Isolated A" in r.stdout, (
        f"isolated_b couldn't reach isolated_a despite both being "
        f"on the same bridge (rc={r.exit_code}, "
        f"stdout={r.stdout_text!r}, stderr={r.stderr_text!r}).  "
        "Either the bridge isn't forwarding (libvirt should bring "
        "it up with stp=on by default — check ``ip link show "
        "<bridge>``), or one of the static IPs on the IsolatedNet "
        "VMs didn't land where expected."
    )

    # 4. isolated_a → mgmtside (cross-network).  isolated_a is
    # ONLY on IsolatedNet; with host_isolated=True there's no
    # gateway on its NIC, so it has no default route.  Even if it
    # had one, the host isn't routing between the two bridges.
    # curl from isolated_a to mgmtside MUST FAIL — and it must
    # fail for a *routing* reason, not a DNS one.  We use the IP
    # form to skip DNS (mgmtside.MgmtNet would NXDOMAIN since
    # IsolatedNet has dns=False, which would be a *different*
    # failure mode than the one we're testing).
    r = isolated_a.exec(
        ["sh", "-c",
         "curl -fsS --max-time 5 http://10.0.0.10/ 2>&1; "
         "echo exit=$?"],
        timeout=15,
    )
    assert b"exit=0" not in r.stdout, (
        f"cross-network breach: isolated_a reached the MgmtNet "
        f"server (10.0.0.10) despite having no gateway and the "
        f"host not bridging the two networks.  curl output: "
        f"{r.stdout_text!r}"
    )


def gen_tests() -> list[Test]:
    user = Credential("root", "testrange")

    return [
        Test(
            Orchestrator(
                networks=[
                    # MgmtNet — standard libvirt-managed network.
                    # Host gets 10.0.0.1 on the bridge; libvirt
                    # NATs outbound; dnsmasq runs DHCP+DNS.
                    VirtualNetwork(
                        "MgmtNet", "10.0.0.0/24",
                        internet=True, dhcp=True, dns=True,
                    ),
                    # IsolatedNet — host_isolated=True ⇒ libvirt
                    # creates the bridge but leaves no host IP on
                    # it.  dnsmasq doesn't run (no IP to bind);
                    # no DHCP / DNS / NAT.  Every VM on this
                    # network must declare a static ``ip=``.
                    VirtualNetwork(
                        "IsolatedNet", "10.99.0.0/24",
                        host_isolated=True,
                        # The constructor REQUIRES these to be False
                        # alongside host_isolated=True — passing them
                        # explicitly so the example's intent is
                        # visible at the call site.
                        dhcp=False, dns=False, internet=False,
                    ),
                ],
                vms=[
                    # mgmtside — proves the control case.  The
                    # runner can curl this VM by FQDN (libvirt's
                    # dnsmasq registers ``mgmtside.MgmtNet`` →
                    # the deterministic-pick IP).
                    VM(
                        name="mgmtside",
                        iso=DEBIAN_CLOUD,
                        users=[user],
                        pkgs=[Apt("nginx")],
                        post_install_cmds=[
                            "rm -f /var/www/html/index.nginx-debian.html",
                            "echo 'Mgmt-side server' "
                            "> /var/www/html/index.html",
                            "systemctl enable --now nginx",
                        ],
                        # Default communicator (guest-agent on
                        # libvirt) is fine — we'll curl this VM
                        # from the runner via its libvirt-bridge
                        # IP, but the runner-to-VM exec path
                        # uses guest-agent under the hood.
                        devices=[
                            vCPU(1), Memory(0.5), HardDrive(10),
                            # Pinned IP so verify() can curl by
                            # address from the runner without
                            # needing libvirt-dnsmasq DNS in the
                            # host's resolv.conf.  10.0.0.10 is
                            # the first DHCP-range address — the
                            # same one deterministic-pick would
                            # have chosen anyway.
                            vNIC("MgmtNet", ip="10.0.0.10"),
                        ],
                    ),
                    # isolated_a — the target of the negative
                    # runner-to-VM assertion.  Static IP because
                    # IsolatedNet has dhcp=False (forced by
                    # host_isolated=True).
                    VM(
                        name="isolated_a",
                        iso=DEBIAN_CLOUD,
                        users=[user],
                        pkgs=[Apt("nginx"), Apt("curl")],
                        post_install_cmds=[
                            "rm -f /var/www/html/index.nginx-debian.html",
                            "echo 'Isolated A' > /var/www/html/index.html",
                            "systemctl enable --now nginx",
                        ],
                        # Must use guest-agent: SSH would need a
                        # routable IP, and the runner has none on
                        # this bridge by design.  guest-agent
                        # rides virtio-serial; works regardless
                        # of IP routing.
                        communicator="guest-agent",
                        devices=[
                            vCPU(1), Memory(0.5), HardDrive(10),
                            vNIC("IsolatedNet", ip="10.99.0.5"),
                        ],
                    ),
                    # isolated_b — proves intra-bridge L2 works.
                    # Same network as isolated_a; runs the curl
                    # for assertion 3.
                    VM(
                        name="isolated_b",
                        iso=DEBIAN_CLOUD,
                        users=[user],
                        pkgs=[Apt("curl")],
                        communicator="guest-agent",
                        devices=[
                            vCPU(1), Memory(0.5), HardDrive(10),
                            vNIC("IsolatedNet", ip="10.99.0.6"),
                        ],
                    ),
                ],
            ),
            verify,
            name="host-isolated-network-v0",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
