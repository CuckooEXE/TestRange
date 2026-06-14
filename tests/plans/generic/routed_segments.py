"""generic/routed_segments: a guest routing traffic between two air-gapped segments.

WHAT: two fully air-gapped switches (no uplink, no sidecar) joined only by a
dual-homed ``router`` guest with IP forwarding baked at build time. A ``client``
on segment A reaches an nginx ``server`` on segment B exclusively through the
router; a TTL check pins the path to exactly one forwarding hop; an on-link
probe proves the segments are genuinely distinct L2 domains; toggling
``ip_forward`` off severs the path (and back on restores it). The client also
carries an ``addr=None`` NIC on segment B, certifying that an unconfigured NIC
exists but stays addressless rather than becoming an accidental L2 shortcut.

WHY: every other plan uses multi-NIC guests as endpoints — nothing certifies
that a backend's L2 segments carry *transit* traffic a guest forwards between
them (distinct MAC/port behavior on some virtual switches), that two bare
switches are actually distinct broadcast domains, that static-only segments
with no sidecar work at all as route targets, or the ``addr=None`` contract.
Routes are added guest-side at test time because ``StaticAddr`` deliberately
carries no static-route knob — the framework renders only a default route.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/routed_segments.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt

# Statics referenced by both the PLAN and the TESTS (routes, curl targets).
_ROUTER_A = "10.61.0.100"
_ROUTER_B = "10.62.0.100"
_CLIENT_A = "10.61.0.110"
_SERVER_B = "10.62.0.110"

# The PosixCred is for ESXi VMware Tools guest-ops, which authenticate per call
# (CORE-60); QGA backends ignore it. admin=True grants the passwordless sudo the
# route/sysctl test steps need when the agent runs unprivileged (ESXi).
_ADMIN = PosixCred("admin", password="testrange", admin=True)

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
pool1 = hyp.add_pool(StoragePool("pool1", 32))
hyp.add_switch(Switch("seg-a", Network("a-net"), cidr="10.61.0.0/24"))
hyp.add_switch(Switch("seg-b", Network("b-net"), cidr="10.62.0.0/24"))
a_net = hyp.networks["a-net"]
b_net = hyp.networks["b-net"]
hyp.vm(
    "router",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    nics=[
        NetworkIface(a_net, StaticAddr(_ROUTER_A)),
        NetworkIface(b_net, StaticAddr(_ROUTER_B)),
    ],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[_ADMIN],
        post_install_commands=(
            # Baked, not test-time: the first test asserts forwarding
            # is on after a plain boot, proving the image carries it.
            "sh -c 'echo net.ipv4.ip_forward=1 > /etc/sysctl.d/99-router.conf'",
        ),
    ),
    communicator=NativeCommunicator(),
)
hyp.vm(
    "client",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    nics=[
        NetworkIface(a_net, StaticAddr(_CLIENT_A)),
        NetworkIface(b_net),
    ],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[_ADMIN],
        packages=[Apt("curl")],
    ),
    communicator=NativeCommunicator(),
)
hyp.vm(
    "server",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    # Explicit-prefix StaticAddr form; the bare form derives
    # its prefix from the Switch CIDR elsewhere in this plan.
    nics=[NetworkIface(b_net, StaticAddr(f"{_SERVER_B}/24"))],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[_ADMIN],
        packages=[Apt("nginx")],
        post_install_commands=(
            "sh -c 'echo routed-hello > /var/www/html/index.html'",
            "systemctl enable --now nginx",
        ),
    ),
    communicator=NativeCommunicator(),
)

PLAN = Plan("routed-segments", hyp)


def _ensure_routes(orch: OrchestratorHandle) -> None:
    # Guest-side /24 routes toward the far segment via the router; `replace` is
    # idempotent so every path test can call this regardless of ordering.
    r = orch.vms["client"].communicator.execute(
        ["sudo", "-n", "ip", "route", "replace", "10.62.0.0/24", "via", _ROUTER_A]
    )
    assert r.ok, f"adding the client's route via the router failed: {r}"
    r = orch.vms["server"].communicator.execute(
        ["sudo", "-n", "ip", "route", "replace", "10.61.0.0/24", "via", _ROUTER_B]
    )
    assert r.ok, f"adding the server's return route via the router failed: {r}"


def router_forwarding_is_baked_into_the_image(orch: OrchestratorHandle) -> None:
    com = orch.vms["router"].communicator
    # /proc, not the sysctl binary: sbin is not on the unprivileged guest-ops
    # PATH on ESXi, and the kernel file is the live truth anyway.
    r = com.execute(["cat", "/proc/sys/net/ipv4/ip_forward"])
    assert r.stdout.strip() == b"1", f"ip_forward not enabled at boot: {r}"
    # Pin the cause to the baked drop-in, not test ordering: a later test
    # toggles the runtime value, so the runtime read alone could go stale-green
    # under a reorder while the image lost the bake.
    r = com.execute(["cat", "/etc/sysctl.d/99-router.conf"])
    assert b"net.ipv4.ip_forward=1" in r.stdout, f"sysctl drop-in missing from the image: {r}"


def unconfigured_nic_exists_but_stays_addressless(orch: OrchestratorHandle) -> None:
    com = orch.vms["client"].communicator
    # Existence first — without it, a driver that silently dropped the addr=None
    # NIC would pass the addressless assert vacuously.
    links = [
        ln
        for ln in com.execute(["ip", "-o", "link"]).stdout.decode().splitlines()
        if ": lo:" not in ln
    ]
    assert len(links) == 2, f"client must carry both declared NICs: {links!r}"
    out = com.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
    addrs = [ln for ln in out.splitlines() if " lo " not in ln]
    assert len(addrs) == 1 and _CLIENT_A in addrs[0], (
        f"client must carry exactly its segment-A static (addr=None NIC stays bare): {addrs!r}"
    )
    # Pin the render contract at the artifact: addr=None must not render as
    # DHCP. The live address check alone cannot discriminate on this DHCP-less
    # segment (a wrongly-DHCP NIC also ends up addressless), and networkd's
    # setup state cannot either (an unconfigured NIC sits in 'configuring' on
    # this stack even with dhcp4 correctly off — live-found). This client
    # declares no DHCP NIC at all, so the rendered netplan must carry none.
    r = com.execute(["sudo", "-n", "sh", "-c", "cat /etc/netplan/*.yaml"])
    assert r.ok, f"reading the rendered netplan failed: {r}"
    assert b"dhcp4: true" not in r.stdout, f"addr=None rendered as a DHCP NIC: {r.stdout!r}"


def segments_are_distinct_l2_domains(orch: OrchestratorHandle) -> None:
    # The plan's premise is that A and B are joined ONLY by the router. Every
    # path test steers by route tables, which would also work on a collapsed
    # L2 — so pin the premise directly: force the server's address on-link on
    # the client's segment-A leg; ARP must fail there. On a backend that wired
    # both networks onto one bridge, the server answers and this probe succeeds.
    com = orch.vms["client"].communicator
    # NIC naming differs per machine type — derive the a-leg iface from its addr.
    out = com.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
    iface = next(ln.split()[1] for ln in out.splitlines() if _CLIENT_A in ln)
    r = com.execute(["sudo", "-n", "ip", "route", "replace", f"{_SERVER_B}/32", "dev", iface])
    assert r.ok, f"pinning the on-link probe route failed: {r}"
    try:
        r = com.execute(["ping", "-c", "1", "-W", "2", _SERVER_B], timeout=15.0)
        assert not r.ok, "server answered on the client's segment-A L2 — segments are collapsed"
    finally:
        com.execute(["sudo", "-n", "ip", "route", "del", f"{_SERVER_B}/32"])


def client_reaches_server_through_the_router(orch: OrchestratorHandle) -> None:
    _ensure_routes(orch)
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_SERVER_B}/"], timeout=20.0
    )
    assert r.ok and b"routed-hello" in r.stdout, f"client could not reach the server: {r}"


def forwarded_path_crosses_exactly_one_hop(orch: OrchestratorHandle) -> None:
    # The server answers with TTL 64; one forwarding hop decrements it to 63. A
    # direct L2 answer (e.g. via the client's unconfigured segment-B NIC) would
    # arrive with ttl=64, so this pins the path to the router. Three echoes so
    # one cold-ARP/nested-scheduling drop cannot flake the cert.
    _ensure_routes(orch)
    r = orch.vms["client"].communicator.execute(
        ["ping", "-c", "3", "-W", "5", _SERVER_B], timeout=30.0
    )
    assert r.ok, f"ping across the routed path failed: {r}"
    assert b"ttl=63" in r.stdout.lower(), f"reply TTL is not one hop off 64: {r.stdout!r}"


def disabling_forwarding_severs_the_path(orch: OrchestratorHandle) -> None:
    _ensure_routes(orch)
    client = orch.vms["client"].communicator
    router = orch.vms["router"].communicator
    ok_probe = ["curl", "-s", "-o", "/dev/null", "--max-time", "10", f"http://{_SERVER_B}/"]
    # The severed probe keeps a short deadline — it exists to convert the
    # silent drop into a quick exit 28; the positive controls get the corpus'
    # normal 10s budget so a slow nested path can't fail them spuriously.
    severed_probe = ["curl", "-s", "-o", "/dev/null", "--max-time", "5", f"http://{_SERVER_B}/"]
    assert client.execute(ok_probe, timeout=20.0).ok, "positive control before the toggle failed"
    r = router.execute(["sudo", "-n", "sysctl", "-w", "net.ipv4.ip_forward=0"])
    assert r.ok, f"disabling ip_forward failed: {r}"
    try:
        r = client.execute(severed_probe, timeout=15.0)
        # A router that stops forwarding drops silently — no ICMP error, no RST —
        # so curl must die on its own clock (exit 28), not exit 7 (refused).
        assert r.exit_code == 28, f"path not severed by ip_forward=0 (curl exit {r.exit_code})"
    finally:
        r = router.execute(["sudo", "-n", "sysctl", "-w", "net.ipv4.ip_forward=1"])
        assert r.ok, f"re-enabling ip_forward failed: {r}"
    assert client.execute(ok_probe, timeout=20.0).ok, "path did not recover after re-enable"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    router_forwarding_is_baked_into_the_image,
    unconfigured_nic_exists_but_stays_addressless,
    segments_are_distinct_l2_domains,
    client_reaches_server_through_the_router,
    forwarded_path_crosses_exactly_one_hop,
    disabling_forwarding_severs_the_path,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
