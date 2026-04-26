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
    └── OuterNet (10.0.0.0/24, internet=True)
        ├── proxmox @ 10.0.0.10   (PVE Hypervisor, auto-installed
        │   │                      via ProxmoxAnswerBuilder)
        │   └── Inner (L2, ProxmoxOrchestrator on the PVE API):
        │       ├── PublicNet  (10.42.0.0/24, internet=True)
        │       │   ├── webpublic @ 10.42.0.5  (nginx)
        │       │   └── client   @ 10.42.0.6  (dual-homed)
        │       └── PrivateNet (10.43.0.0/24, internet=False)
        │           ├── dbprivate @ 10.43.0.5  (nginx)
        │           └── client    @ 10.43.0.6  (dual-homed)
        └── sidecar @ 10.0.0.11   (Debian + curl, smoke tests the
                                   ProxMox API over HTTPS/8006)

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

Prerequisites
=============

- KVM on the physical host (nested-virt support is only required
  once the inner orchestrator is re-enabled — v0 doesn't boot any
  inner VMs).
- A running ssh-agent (``echo $SSH_AUTH_SOCK`` returns a path).
  This example generates a fresh ed25519 keypair per run and loads
  it via ``ssh-add`` so the test runner can SSH into the booted PVE
  + sidecar without ever touching the user's ``~/.ssh/`` keys.  No
  pre-existing key required.
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
    Memory,
    Orchestrator,
    Test,
    VirtualNetwork,
    vNIC,
    run_tests,
    vCPU,
)
from testrange.backends.libvirt import Hypervisor
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


def _verify_inner_reachability(orch: Orchestrator) -> None:
    """Inner-layer reachability: L2 → L2 + L2 → L1.

    Mirrors the assertions the libvirt-on-libvirt counterpart in
    :mod:`examples.nested_public_private` makes, just with the
    inner orchestrator coming from
    :meth:`ProxmoxOrchestrator.root_on_vm` instead of libvirt.

    1. The outer libvirt orchestrator stashed any inner
       orchestrators it entered on ``_inner_orchestrators``.  Pull
       the PVE-rooted one out — there's exactly one in this
       example.
    2. From the dual-homed inner ``client``, curl
       ``http://10.42.0.5/`` reaches the L2 public webserver.
       Failure here means the inner ``ProxmoxOrchestrator`` either
       didn't provision the network or didn't bring the inner
       ``webpublic`` up.
    3. From the same client on its private NIC, curl
       ``http://10.43.0.5/`` reaches the L2 private DB.  This
       proves cross-network reachability *within* the inner layer
       and that the ``PrivateNet`` (``internet=False``) doesn't
       isolate inner peers from each other.
    """
    inner_orchestrators = getattr(orch, "_inner_orchestrators", [])
    assert inner_orchestrators, (
        "no inner orchestrator entered — outer libvirt orchestrator "
        "did not detect the Hypervisor or root_on_vm raised."
    )
    inner = inner_orchestrators[0]
    client = inner.vms["client"]

    pub = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://10.42.0.5/"],
        timeout=20,
    ).check()
    assert b"Public webserver" in pub.stdout, pub.stdout_text

    priv = client.exec(
        ["curl", "-fsS", "--max-time", "10", "http://10.43.0.5/"],
        timeout=20,
    ).check()
    assert b"Private DB" in priv.stdout, priv.stdout_text


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
                            vNIC("OuterNet", ip="10.0.0.11"),
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
                            vNIC("OuterNet", ip="10.0.0.10"),
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
                                    Memory(0.5),
                                    HardDrive(10),
                                    vNIC(
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
                                    Memory(0.5),
                                    HardDrive(10),
                                    vNIC(
                                        "PrivateNet", ip="10.43.0.5",
                                    ),
                                ],
                            ),
                            VM(
                                name="client",
                                iso=DEBIAN_CLOUD,
                                users=[root_cred],
                                pkgs=[Apt("curl"), Apt("iputils-ping")],
                                devices=[
                                    vCPU(1),
                                    Memory(0.5),
                                    HardDrive(10),
                                    vNIC(
                                        "PublicNet", ip="10.42.0.6",
                                    ),
                                    vNIC(
                                        "PrivateNet", ip="10.43.0.6",
                                    ),
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
