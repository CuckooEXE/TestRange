"""ProxMox VE as a nested L1 hypervisor with inner L2 VMs.

ProxMox-flavoured counterpart of :mod:`examples.nested_public_private`:
the outer libvirt orchestrator boots a PVE installer unattended into
a cached image, the resulting :class:`Hypervisor` VM runs
``pveproxy`` on its REST endpoint, and a nested
:class:`ProxmoxOrchestrator` provisions inner networks + VMs against
that endpoint.

Topology
========

::

    Outer (L1, libvirt):
    └── OuterNet (10.0.0.0/24, internet=True, dhcp=True)
        ├── sidecar     (DHCP, .2 — Debian + curl, smoke tests the
        │                ProxMox API over HTTPS/8006)
        └── proxmox     (DHCP, .3 — PVE Hypervisor, auto-installed
            │                       via ProxmoxAnswerBuilder)
            └── Inner (L2, ProxmoxOrchestrator on the PVE API):
                ├── PublicNet  (10.42.0.0/24, internet=True, dhcp=True)
                │   ├── webpublic   (DHCP, nginx)
                │   └── client      (DHCP, dual-homed; primary NIC)
                └── PrivateNet (10.43.0.0/24, internet=False, dhcp=False)
                    ├── dbprivate @ 10.43.0.5   (static, nginx)
                    └── client    @ 10.43.0.6   (static, secondary NIC)

DHCP-enabled networks let TestRange's deterministic-pick assign each
NIC its address (Nth declared VM lands on Nth host address — see
:doc:`/usage/networks` "DHCP-discovery vNICs").  ``PrivateNet`` has
``dhcp=False`` so it requires explicit ``ip=`` on every NIC.

What the example demonstrates
=============================

1. The :class:`ProxmoxAnswerBuilder` + pure-Python
   :mod:`testrange.vms.builders._proxmox_prepare` pipeline boots a
   vanilla PVE ISO unattended and lands in a cached post-install
   image exactly like CloudInitBuilder does for Debian.
2. A sibling Debian VM on the same outer network can reach the
   ProxMox API — the canonical first hop for anything that'll drive
   ProxMox over its REST API.
3. :meth:`ProxmoxOrchestrator.root_on_vm` constructs an inner
   orchestrator pointing at the PVE VM's REST endpoint, which the
   outer orchestrator's :class:`ExitStack` enters as a normal
   nested orchestrator.  The inner orchestrator then provisions
   the L2 networks + VMs declared on the :class:`Hypervisor`.
4. The inner VMs talk to the test runner over PVE's
   :class:`~testrange.backends.proxmox.guest_agent.ProxmoxGuestAgentCommunicator`,
   so the L2 SDN subnets (10.42/24, 10.43/24) do **not** need to
   be routed back to the outer host — agent traffic hops through
   PVE's local virtio-serial channel.

Prerequisites
=============

- KVM on the physical host with nested-virt enabled (the inner
  L2 layer is real KVM-on-KVM-on-KVM).
- A running ssh-agent (``echo $SSH_AUTH_SOCK`` returns a path).
  This example generates a fresh ed25519 keypair per run and loads
  it via ``ssh-add`` so the test runner can SSH into the booted PVE
  + sidecar without ever touching the user's ``~/.ssh/`` keys.  No
  pre-existing key required.  (The outer ``proxmox`` VM uses SSH
  on its routable 10.0.0.10 outer-bridge IP; only the inner L2
  VMs use the guest-agent path.)
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
    Hypervisor,
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)
from testrange.backends.proxmox import ProxmoxOrchestrator

DEBIAN_CLOUD = (
    "https://cloud.debian.org/images/cloud/bookworm/latest/" "debian-12-generic-amd64.qcow2"
)

# ProxMox VE installer ISO.  Pinned to a specific 9.x release so the
# prepared-ISO cache key stays stable across runs — bump as needed.
# The registry auto-selects ``ProxmoxAnswerBuilder`` (UEFI by default)
# for any ISO whose filename matches ``proxmox-ve[-_]*.iso``.
PROXMOX_ISO = "https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso"

def _generate_run_keypair() -> str:
    """Generate a fresh ed25519 keypair for this run, write the
    private key to a temp file, load it into ``ssh-agent``, and
    return the public-key string.

    The pubkey lands in ``answer.toml`` (PVE) and the sidecar's
    cloud-init ``user-data`` (Debian); the matching private key in
    the agent lets paramiko's
    :class:`~testrange.communication.ssh.SSHCommunicator` connect
    without ever touching ``~/.ssh/``.

    Cleaned up on interpreter exit (``atexit``):
    ``ssh-add -d`` removes it from the agent and the temp file is
    unlinked, so repeated runs don't accumulate cruft.
    """
    import atexit
    import os
    import subprocess
    import tempfile

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    if "SSH_AUTH_SOCK" not in os.environ:
        raise RuntimeError(
            "ssh-agent is not running.  Start one before this "
            "example so the per-run key can be loaded — e.g. "
            "`eval $(ssh-agent -s)`."
        )

    private_key = Ed25519PrivateKey.generate()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")

    fd, key_path = tempfile.mkstemp(prefix="testrange-runkey-", suffix=".key")
    os.close(fd)
    os.chmod(key_path, 0o600)
    with open(key_path, "wb") as fh:
        fh.write(private_pem)

    add = subprocess.run(
        ["ssh-add", key_path],
        capture_output=True, text=True, timeout=10,
    )
    if add.returncode != 0:
        os.unlink(key_path)
        raise RuntimeError(
            f"ssh-add {key_path} failed (exit {add.returncode}): "
            f"{add.stderr.strip()}"
        )

    def _cleanup() -> None:
        subprocess.run(
            ["ssh-add", "-d", key_path],
            capture_output=True, timeout=10, check=False,
        )
        try:
            os.unlink(key_path)
        except OSError:
            pass

    atexit.register(_cleanup)
    return f"{public_openssh.strip()} testrange-run"


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

    # 3. Sidecar reaches the ProxMox API by FQDN.  PVE 9.x requires
    #    a valid ticket for ``/api2/json/version`` (an older
    #    unauthenticated endpoint is no longer public), so 401 is
    #    the normal response for an un-authed probe.  Both 200 and
    #    401 prove pveproxy is alive and routing — which is the L1
    #    smoke we care about; authenticated access is a separate
    #    concern to prove in a dedicated auth test.
    #
    #    ``proxmox.OuterNet`` is the FQDN libvirt's bridge-local
    #    dnsmasq registers for the proxmox VM (deterministic-pick
    #    gave it a stable IP via the OuterNet DHCP reservation, and
    #    libvirt's dnsmasq exposes that as the VM's <name>.<network>
    #    A record).  Sidecar's resolv.conf already lists OuterNet's
    #    gateway as its DNS, so this resolves end-to-end without us
    #    spelling out the IP.
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
            "https://proxmox.OuterNet:8006/api2/json/version",
        ],
        timeout=20,
    ).check()
    assert r.stdout.strip() in (b"200", b"401"), (
        f"sidecar curl to PVE API returned {r.stdout_text!r}, expected "
        "200 (pre-9.x unauthenticated) or 401 (9.x auth-required)"
    )


def _log_inner_state(orch) -> None:
    """Dump per-inner-VM diagnostics so a failing reachability
    assertion prints actionable context instead of just "curl: (7)".

    Specifically: each VM's network state (``ip -br addr`` /
    ``ip route``), nginx status on the two server VMs, and the
    set of listening sockets.  Output goes through ``_log`` at
    INFO so it shows up alongside the orchestrator's own lifecycle
    lines without needing ``--log-level debug``.
    """
    import logging
    log = logging.getLogger("examples.nested_proxmox")
    log.setLevel(logging.INFO)

    inner_orchestrators = getattr(orch, "_inner_orchestrators", [])
    if not inner_orchestrators:
        log.warning("no inner orchestrator entered — skipping diagnostics")
        return
    inner = inner_orchestrators[0]

    def _run(vm_name: str, argv: list[str]) -> str:
        try:
            r = inner.vms[vm_name].exec(argv, timeout=10)
            return (
                f"  exit={r.exit_code} "
                f"stdout={r.stdout_text.strip()!r} "
                f"stderr={r.stderr_text.strip()!r}"
            )
        except Exception as exc:
            return f"  exec({argv}) raised: {exc}"

    for vm_name in ("webpublic", "dbprivate", "client"):
        log.info("=== inner VM %r ===", vm_name)
        log.info(" hostname:")
        log.info(_run(vm_name, ["hostname"]))
        log.info(" ip -br addr:")
        log.info(_run(vm_name, ["ip", "-br", "addr"]))
        log.info(" ip route:")
        log.info(_run(vm_name, ["ip", "route"]))
        if vm_name in ("webpublic", "dbprivate"):
            log.info(" systemctl is-active nginx:")
            log.info(_run(vm_name, ["systemctl", "is-active", "nginx"]))
            log.info(" ss -tlnp (listening sockets):")
            log.info(_run(vm_name, ["ss", "-tlnp"]))


def _verify_inner_reachability(orch: Orchestrator) -> None:
    """Inner-layer reachability: L2 → L2 + L2 → L1, IP and FQDN.

    Mirrors the assertions the libvirt-on-libvirt counterpart in
    :mod:`examples.nested_public_private` makes, just with the
    inner orchestrator coming from
    :meth:`ProxmoxOrchestrator.root_on_vm` instead of libvirt.

    1. The outer libvirt orchestrator stashed any inner
       orchestrators it entered on ``_inner_orchestrators``.  Pull
       the PVE-rooted one out — there's exactly one in this
       example.
    2. From the dual-homed inner ``client``, curl
       ``http://webpublic.PublicNet/`` reaches the L2 public
       webserver by FQDN.  PublicNet is the client's primary NIC,
       so its /etc/resolv.conf lists PublicNet's dnsmasq first —
       the FQDN resolves via the IPAM-backed ``dhcp-host`` record
       TestRange pushed at __enter__ to the IP webpublic actually
       boots on (no manual ``ip=`` was specified — both VMs use
       DHCP-discovery on PublicNet).  Failure here means either
       the inner ``ProxmoxOrchestrator`` didn't provision the
       network, didn't bring ``webpublic`` up, or PVE's per-vnet
       dnsmasq didn't pick up the IPAM entries.
    3. From the same client to ``http://10.43.0.5/`` (the
       static-IP DB on PrivateNet).  This stays IP-based: the
       client's primary NIC is PublicNet, so its first resolv.conf
       entry is PublicNet's dnsmasq, which doesn't know
       ``dbprivate.PrivateNet`` and answers NXDOMAIN (glibc takes
       NXDOMAIN as authoritative — no fallback to the second
       nameserver).  Each per-vnet dnsmasq's IPAM is its own DNS
       scope; cross-vnet FQDN lookups are explicitly tested below
       in step 4 against the matching vnet's gateway.  The IP curl
       still proves cross-network reachability *within* the inner
       layer and that ``PrivateNet`` (``internet=False``) doesn't
       isolate inner peers from each other.
    4. **FQDN DNS via PVE's per-vnet dnsmasq.**  ``host`` against
       each vnet's gateway proves the IPAM→dnsmasq path directly,
       returning the IP TestRange registered for each FQDN.  This
       is the assertion that pins libvirt-parity DNS behaviour;
       an IP-only verify would let DHCP-without-DNS slip through.

    On failure, dumps every inner VM's network + service state
    (see :func:`_log_inner_state`) before re-raising so the
    teardown log carries enough context to localise the problem.
    """
    # Always dump the diagnostics first.  They're cheap (single
    # guest-agent exec per command) and make the logs useful even
    # when everything succeeds — easy way to confirm IPs landed
    # on the right NICs and nginx is up.
    _log_inner_state(orch)

    inner_orchestrators = getattr(orch, "_inner_orchestrators", [])
    assert inner_orchestrators, (
        "no inner orchestrator entered — outer libvirt orchestrator "
        "did not detect the Hypervisor or root_on_vm raised."
    )
    inner = inner_orchestrators[0]
    client = inner.vms["client"]

    pub = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://webpublic.PublicNet/"],
        timeout=20,
    ).check()
    assert b"Public webserver" in pub.stdout, pub.stdout_text

    # PrivateNet stays IP-based: see docstring step 3 — cross-vnet
    # FQDN via /etc/resolv.conf NXDOMAINs; the explicit-server
    # version is in step 4.
    priv = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://10.43.0.5/"],
        timeout=20,
    ).check()
    assert b"Private DB" in priv.stdout, priv.stdout_text

    # 4. FQDN resolution — proves PVE's per-vnet dnsmasq is
    #    answering for the IPAM-registered hostnames TestRange
    #    pushed at __enter__.  ``host`` (from dnsutils, which the
    #    client gets via its package list) takes an explicit DNS
    #    server as its second argument so each query lands on the
    #    matching vnet's dnsmasq directly — bypassing the
    #    cloud-init-configured /etc/resolv.conf order, which on a
    #    multi-NIC VM depends on NIC declaration order and would
    #    NXDOMAIN cross-vnet lookups (each vnet's IPAM is its own
    #    DNS scope).  We verify both the lookup succeeds AND
    #    returns the IP we registered, then curl by FQDN as the
    #    end-to-end check.
    # Public lookup — webpublic auto-allocated to 10.42.0.2 by the
    # deterministic-pick (it's the first VM declared on PublicNet,
    # so it lands on the first non-gateway host).  The dbprivate VM
    # stays at its declared static 10.43.0.5 because PrivateNet is
    # ``dhcp=False``.
    pub_lookup = client.exec(
        ["host", "webpublic.PublicNet", "10.42.0.1"],
        timeout=15,
    ).check()
    assert b"10.42.0.2" in pub_lookup.stdout, pub_lookup.stdout_text

    priv_lookup = client.exec(
        ["host", "dbprivate.PrivateNet", "10.43.0.1"],
        timeout=15,
    ).check()
    assert b"10.43.0.5" in priv_lookup.stdout, priv_lookup.stdout_text


def verify(orch: Orchestrator) -> None:
    """Outer-layer smoke + inner-layer reachability."""
    _verify_outer_layer(orch)
    _verify_inner_reachability(orch)


def _nginx_post_install(body: str) -> list[str]:
    return [
        "rm -f /var/www/html/index.nginx-debian.html",
        f"echo '<h1>{body}</h1>' > /var/www/html/index.html",
        "systemctl enable --now nginx",
    ]


def gen_tests() -> list[Test]:
    root_cred = Credential(
        "root", "testrange", ssh_key=_generate_run_keypair(),
    )

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
                            # No ip= — OuterNet has dhcp=True, so the
                            # orchestrator's deterministic-pick assigns
                            # the first non-gateway host (10.0.0.2) and
                            # libvirt's dnsmasq registers
                            # ``sidecar.OuterNet`` as the FQDN.
                            vNIC("OuterNet"),
                        ],
                    ),
                    # ProxMox VE Hypervisor — auto-installed via
                    # ProxmoxAnswerBuilder, then driven by an inner
                    # ProxmoxOrchestrator that the outer libvirt
                    # orchestrator enters via root_on_vm().  The
                    # registry auto-selects ``ProxmoxAnswerBuilder``
                    # for any ISO whose filename matches
                    # ``proxmox-ve[-_]*.iso``; UEFI (OVMF) is the
                    # default because BIOS-mode GRUB triple-faults
                    # on the SATA-CD attach pattern.
                    Hypervisor(
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
                            # want to actually run more inner VMs.
                            Memory(4),
                            # Min recommended is 8 GiB, but the
                            # installer balks on <32 GiB at its
                            # default LVM layout.  64 GiB is the
                            # smallest round number that doesn't
                            # trip any of PVE's sanity checks.
                            HardDrive(64),
                            # No ip= — same DHCP-discovery story as
                            # sidecar.  Declared second on OuterNet so
                            # it picks 10.0.0.3, and ``proxmox.OuterNet``
                            # becomes the libvirt-dnsmasq-resolvable
                            # FQDN that ``_verify_outer_layer`` curls
                            # at port 8006.
                            vNIC("OuterNet"),
                        ],
                        # ProxmoxOrchestrator.root_on_vm() will
                        # construct an inner orchestrator pointing
                        # at this VM's REST endpoint and provision
                        # the networks + vms below inside it.
                        orchestrator=ProxmoxOrchestrator,
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
                            # Inner VMs use ``communicator='guest-agent'``
                            # explicitly: PVE's REST ``/agent/`` endpoints
                            # let the outer test runner talk to each L2
                            # VM through PVE's host-mediated virtio-serial
                            # channel, so the SDN subnets don't need to
                            # be routable from the outer host.  This is
                            # the cloud-init default already, but spell
                            # it out here so the contract is visible.
                            VM(
                                name="webpublic",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                pkgs=[Apt("nginx")],
                                post_install_cmds=_nginx_post_install(
                                    "Public webserver"
                                ),
                                communicator="guest-agent",
                                devices=[
                                    vCPU(1),
                                    Memory(0.5),
                                    HardDrive(10),
                                    # PublicNet has dhcp=True; no ip=,
                                    # deterministic-pick lands this VM
                                    # on 10.42.0.2 (first non-gateway
                                    # host).  PVE's per-vnet dnsmasq
                                    # serves ``webpublic.PublicNet`` →
                                    # 10.42.0.2 from the IPAM entry
                                    # TestRange writes at __enter__.
                                    vNIC("PublicNet"),
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
                                communicator="guest-agent",
                                devices=[
                                    vCPU(1),
                                    Memory(0.5),
                                    HardDrive(10),
                                    # PrivateNet has dhcp=False, so
                                    # every NIC needs an explicit ip=.
                                    # Static still flows through to
                                    # PVE's IPAM, so
                                    # ``dbprivate.PrivateNet`` resolves
                                    # via PrivateNet's dnsmasq exactly
                                    # the same way the DHCP-discovered
                                    # peers on PublicNet do.
                                    vNIC("PrivateNet", ip="10.43.0.5"),
                                ],
                            ),
                            VM(
                                name="client",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                # ``dnsutils`` ships ``host`` for the
                                # FQDN lookups in
                                # ``_verify_inner_reachability``.
                                pkgs=[
                                    Apt("curl"),
                                    Apt("iputils-ping"),
                                    Apt("dnsutils"),
                                ],
                                communicator="guest-agent",
                                devices=[
                                    vCPU(1),
                                    Memory(0.5),
                                    HardDrive(10),
                                    # PublicNet (dhcp=True) — no ip=,
                                    # picks 10.42.0.3 (after webpublic
                                    # at .2).  PrivateNet (dhcp=False)
                                    # needs a static ip=.  Declaration
                                    # order matters: PublicNet first so
                                    # the client's resolv.conf lists
                                    # PublicNet's dnsmasq first, which
                                    # is what makes ``http://webpublic
                                    # .PublicNet/`` resolve in step 2
                                    # of _verify_inner_reachability.
                                    vNIC("PublicNet"),
                                    vNIC("PrivateNet", ip="10.43.0.6"),
                                ],
                            ),
                        ],
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
