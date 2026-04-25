"""ProxMox VE as an L1 guest with a sibling sidecar L1 peer (v0, non-nested).

This example is the ProxMox-flavoured counterpart of
:mod:`examples.nested_public_private` — the end-state is a ``Hypervisor``
running ProxMox VE with inner networks and VMs on top, but **v0 stops
short of the nested layer**.  The inner orchestrator, inner networks,
and inner VMs are commented out behind ``TODO(proxmox-nest):`` markers
and light up once the ProxMox backend's ``root_on_vm()`` implementation
lands.

Topology (v0)
=============

::

    Outer (L1):
    └── OuterNet (10.0.0.0/24, internet=True)
        ├── proxmox @ 10.0.0.10   (ProxMox VE VM, auto-installed
        │                          via ProxmoxAnswerBuilder)
        └── sidecar @ 10.0.0.11   (Debian + curl, smoke tests the
                                   ProxMox API over HTTPS/8006)

What v0 demonstrates
====================

1. The :class:`ProxmoxAnswerBuilder` + pure-Python
   :mod:`testrange.vms.builders._proxmox_prepare` pipeline boots a
   vanilla PVE ISO unattended and lands in a cached post-install
   qcow2 exactly like CloudInitBuilder does for Debian.
2. A sibling Debian VM on the same outer network can reach the
   ProxMox API — the canonical first hop for anything that'll drive
   ProxMox over its REST API.

Prerequisites
=============

- KVM on the physical host (nested-virt support is only required
  once the inner orchestrator is re-enabled — v0 doesn't boot any
  inner VMs).
- An SSH public key at ``~/.ssh/id_ed25519.pub`` — written into
  ``answer.toml`` so ``root@<proxmox-ip>`` is reachable without a
  password prompt.
- The vanilla ProxMox VE installer ISO URL.  Upstream mirrors
  require no login for the community installer; pin a specific
  version via :data:`PROXMOX_ISO` below when you iterate.

Running
=======

::

    testrange run examples/nested_proxmox_public_private.py:gen_tests

First run: expect ~15–25 min for the ProxMox install (ISO download +
auto-install).  Second run: cache-hit, boots from the cached disk in
<60 s.
"""

from __future__ import annotations

import time
from pathlib import Path

from testrange import (
    VM,
    Apt,
    Credential,
    HardDrive,
    # Hypervisor,  # TODO(proxmox-nest): re-enable once ProxmoxOrchestrator lands
    # LibvirtOrchestrator,  # TODO(proxmox-nest): inner orchestrator type
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    VirtualNetworkRef,
    run_tests,
    vCPU,
)

DEBIAN_CLOUD = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/" "debian-12-generic-amd64.qcow2"
)

# ProxMox VE installer ISO.  Pinned to a specific 9.x release so the
# prepared-ISO cache key stays stable across runs — bump as needed.
# The registry auto-selects ``ProxmoxAnswerBuilder`` (UEFI by default)
# for any ISO whose filename matches ``proxmox-ve[-_]*.iso``.
PROXMOX_ISO = "https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso"

SSH_PUBLIC_KEY = Path("~/.ssh/id_ed25519.pub").expanduser().read_text().strip()


_PVEPROXY_READY_TIMEOUT_S = 120
"""How long to wait for ``pveproxy`` to finish starting after SSH is up.

PVE's ``pveproxy.service`` depends on ``pve-cluster``, ``pvedaemon``,
and a handful of others that take noticeably longer than ``sshd`` to
reach active state — up to 60s on the first boot of a fresh install
with default perf.  120s is a wide margin; cache-hit reboots usually
converge in 15–30s."""


def _wait_for_pveproxy(proxmox, timeout_s: int = _PVEPROXY_READY_TIMEOUT_S) -> None:
    """Poll ``systemctl is-active pveproxy`` until active or timeout.

    TestRange's run-phase readiness gate is the communicator (SSH or
    guest-agent) on the VM — that fires as soon as ``sshd`` is up,
    which on PVE happens well before the API daemon finishes
    starting.  Without this wait the first curl to ``:8006`` races
    pveproxy's startup and intermittently returns connection refused
    (curl exit 7).
    """
    deadline = time.monotonic() + timeout_s
    last_stderr = b""
    while time.monotonic() < deadline:
        r = proxmox.exec(["systemctl", "is-active", "pveproxy"])
        if r.exit_code == 0 and b"active" in r.stdout:
            return
        last_stderr = r.stderr
        time.sleep(2)
    raise AssertionError(
        f"pveproxy did not become active within {timeout_s}s; "
        f"last stderr: {last_stderr!r}"
    )


def _verify_outer_layer(orch: Orchestrator) -> None:
    """Outer-layer checks: ProxMox VM responds + sidecar reaches the API.

    Four assertions, each of which pins a distinct failure mode:

    1. ``pveversion`` on the ProxMox VM proves the install landed
       cleanly (the binary only exists on a fully installed PVE host)
       *and* that our SSH communicator negotiated against the
       post-install sshd.
    2. Wait for ``pveproxy`` to reach ``active`` — sshd comes up
       well before the API daemon, and racing the API check against
       pveproxy's startup is how early runs flaked with curl exit 7.
    3. From the sidecar, HTTPS to ``:8006`` returns either 200 or
       401.  401 is what PVE 9.x answers on ``/api2/json/version``
       without a ticket (older PVE releases returned 200
       unauthenticated); either proves the API daemon is up and
       routing requests, which is the canonical first hop for
       anything that'll drive ProxMox over its REST API.  A dead
       daemon would be ``Connection refused``; an HTTPS redirector
       would return something outside the PVE-ish status set.
    """
    proxmox = orch.vms["proxmox"]
    sidecar = orch.vms["sidecar"]

    # 1. ProxMox VM is a real PVE install.
    r = proxmox.exec(["pveversion"]).check()
    assert b"pve-manager" in r.stdout, f"pveversion output missing pve-manager: {r.stdout_text!r}"

    # 2. pveproxy is up — pveversion is a static binary lookup that
    #    succeeds before the PVE service stack finishes initialising,
    #    so we must explicitly wait for the API daemon.
    _wait_for_pveproxy(proxmox)

    # 3. Sidecar reaches the ProxMox API.  PVE 9.x requires a valid
    #    ticket for ``/api2/json/version`` (an older unauthenticated
    #    endpoint is no longer public), so 401 is the normal response
    #    for an un-authed probe.  Both 200 and 401 prove pveproxy is
    #    alive and routing — which is the L1 smoke we care about;
    #    authenticated access is a separate concern to prove in a
    #    dedicated auth test.
    r = sidecar.exec(
        [
            "curl",
            "-sk",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--max-time",
            "10",
            "https://10.0.0.10:8006/api2/json/version",
        ],
        timeout=20,
    ).check()
    assert r.stdout.strip() in (b"200", b"401"), (
        f"sidecar curl to PVE API returned {r.stdout_text!r}, expected "
        "200 (pre-9.x unauthenticated) or 401 (9.x auth-required)"
    )


# TODO(proxmox-nest): re-enable once ProxmoxOrchestrator implements
# root_on_vm().  At that point, the hypervisor-wrapper form below
# replaces the plain VM form in gen_tests(), and this helper exercises
# the inner L2 → L2 and L2 → L1 reachability contract the way
# examples/nested_public_private.py does.
#
# def _verify_inner_reachability(orch: Orchestrator) -> None:
#     inner_orchestrators = getattr(orch, "_inner_orchestrators", [])
#     assert inner_orchestrators, "no inner orchestrator entered"
#     inner = inner_orchestrators[0]
#     client = inner.vms["client"]
#     r = client.exec(
#         ["curl", "-fsS", "--max-time", "10", "http://10.42.0.5/"],
#         timeout=20,
#     ).check()
#     assert b"Public webserver" in r.stdout, r.stdout_text


def verify(orch: Orchestrator) -> None:
    """v0 runs the outer-layer smoke test only.

    The inner-layer reachability checks live in the commented-out
    block above and get re-enabled alongside the nested orchestrator.
    """
    _verify_outer_layer(orch)


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
                        "OuterNet",
                        "10.0.0.0/24",
                        internet=True,
                        dhcp=True,
                    ),
                ],
                vms=[
                    # L1 sidecar — Debian with curl so we can poke the
                    # ProxMox API from an external vantage.  Mirrors the
                    # sidecar in examples/nested_public_private.py, but
                    # we don't need nginx here — the sidecar is a
                    # client, not a server, in the v0 smoke test.
                    VM(
                        name="sidecar",
                        iso=DEBIAN_CLOUD,
                        users=[root_cred],
                        pkgs=[Apt("curl"), Apt("iputils-ping")],
                        devices=[
                            vCPU(1),
                            # Sidecar is a tiny Debian client running curl;
                            # 512 MiB is plenty, and every GiB saved here
                            # leaves more headroom under the 85% host-RAM
                            # preflight for the bigger ProxMox VM.
                            Memory(0.5),
                            HardDrive(10),
                            VirtualNetworkRef("OuterNet", ip="10.0.0.11"),
                        ],
                    ),
                    # ProxMox VE VM — the eventual nested hypervisor.
                    # Auto-selected builder via the registry:
                    # ``is_proxmox_installer_iso("proxmox-ve_9.1-1.iso")``
                    # routes to :class:`ProxmoxAnswerBuilder`, which
                    # defaults to UEFI (OVMF) because BIOS-mode GRUB
                    # triple-faults on libvirt's SATA-CD attach.
                    VM(
                        name="proxmox",
                        iso=PROXMOX_ISO,
                        users=[root_cred],
                        communicator="ssh",
                        devices=[
                            vCPU(2),
                            # ProxMox installer requires ≥2 GiB; 4 GiB
                            # is the sweet spot: it leaves room for
                            # the ~600 MiB installer squashfs mounted
                            # to tmpfs plus a couple of GiB for the
                            # kernel, installer processes, and a
                            # running `pve-manager` after install.
                            # Bigger values fail the 85% host-RAM
                            # preflight on a 16 GiB dev box where
                            # the host is already ~8 GiB in.  Bump
                            # to 6–8 GiB on a bigger host if you
                            # want to actually run nested VMs later.
                            Memory(4),
                            # Min recommended is 8 GiB, but the
                            # installer balks on <32 GiB at its
                            # default LVM layout.  64 GiB is the
                            # smallest round number that doesn't
                            # trip any of PVE's sanity checks.
                            HardDrive(64),
                            VirtualNetworkRef("OuterNet", ip="10.0.0.10"),
                        ],
                        # TODO(proxmox-nest): swap VM → Hypervisor
                        # and uncomment the nested plumbing below
                        # once ProxmoxOrchestrator.root_on_vm() is
                        # implemented.  The outer/inner resource
                        # counts, static IPs, and smoke-test hooks
                        # are kept verbatim from
                        # examples/nested_public_private.py so the
                        # diff is just `git diff nested_public_private
                        # nested_proxmox_public_private` minus this
                        # comment block.
                        #
                        # orchestrator=LibvirtOrchestrator,  # NOTE:
                        # will become ProxmoxOrchestrator once
                        # available.  LibvirtOrchestrator running
                        # *inside* a PVE guest works in principle —
                        # PVE ships libvirtd-dev-friendly enough —
                        # but the point of the ProxMox-as-hypervisor
                        # cut is to drive PVE's own API, not layer
                        # libvirt on top of it.
                        #
                        # networks=[
                        #     VirtualNetwork(
                        #         "PublicNet", "10.42.0.0/24",
                        #         internet=True, dhcp=True,
                        #     ),
                        #     VirtualNetwork(
                        #         "PrivateNet", "10.43.0.0/24",
                        #         internet=False, dhcp=False,
                        #     ),
                        # ],
                        # vms=[
                        #     VM(
                        #         name="webpublic",
                        #         iso=DEBIAN_CLOUD,
                        #         users=[root_cred],
                        #         pkgs=[Apt("nginx")],
                        #         post_install_cmds=_nginx_post_install(
                        #             "Public webserver"
                        #         ),
                        #         devices=[
                        #             vCPU(1),
                        #             Memory(0.5),
                        #             HardDrive(10),
                        #             VirtualNetworkRef(
                        #                 "PublicNet", ip="10.42.0.5",
                        #             ),
                        #         ],
                        #     ),
                        #     VM(
                        #         name="dbprivate",
                        #         iso=DEBIAN_CLOUD,
                        #         users=[root_cred],
                        #         pkgs=[Apt("nginx")],
                        #         post_install_cmds=_nginx_post_install(
                        #             "Private DB"
                        #         ),
                        #         devices=[
                        #             vCPU(1),
                        #             Memory(0.5),
                        #             HardDrive(10),
                        #             VirtualNetworkRef(
                        #                 "PrivateNet", ip="10.43.0.5",
                        #             ),
                        #         ],
                        #     ),
                        #     VM(
                        #         name="client",
                        #         iso=DEBIAN_CLOUD,
                        #         users=[root_cred],
                        #         pkgs=[Apt("curl"), Apt("iputils-ping")],
                        #         devices=[
                        #             vCPU(1),
                        #             Memory(0.5),
                        #             HardDrive(10),
                        #             VirtualNetworkRef(
                        #                 "PublicNet", ip="10.42.0.6",
                        #             ),
                        #             VirtualNetworkRef(
                        #                 "PrivateNet", ip="10.43.0.6",
                        #             ),
                        #         ],
                        #     ),
                        # ],
                    ),
                ],
            ),
            verify,
            name="nested-proxmox-public-private-v0",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
