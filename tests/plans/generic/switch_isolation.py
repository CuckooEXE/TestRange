"""generic/switch_isolation: the three switch tiers and a directional reach matrix.

WHAT: three switches, one per tier the model offers. ``sw-a`` is **uplinked** —
``uplink="egress"`` with a NAT+DNS sidecar, so its guests egress and resolve.
``sw-b`` is **air-gapped** — a bare Switch, no uplink, no sidecar, pure L2.
``sw-c`` is **mgmt** — ``mgmt=True`` puts a host adapter at ``.2`` with no uplink
and no sidecar. ``web-a`` is static on ``sw-a`` (its gateway/DNS derive from the
sidecar, so it egresses by content-address, not DHCP); ``web-b`` is static on the
air-gapped ``sw-b``; ``client`` is triple-homed — DHCP on ``sw-a``, static on
``sw-b``, static on ``sw-c`` — so exactly one default route (via ``sw-a``) and an
L2 leg on every tier. The ten tests walk the full directional matrix and are
written to pin the *provenance* of each result, not just its pass/fail: the
triple-homed client reaches a peer on each switch (web-a by DNS over the uplinked
one, web-b by IP over the air-gapped one, the mgmt adapter **over its c1 leg** —
proven via ``ip route get`` so the NAT path can't stand in for it) and the
internet; the uplinked web egresses by its sidecar-*derived* gateway; web-b
serves locally (a positive control); and the air-gapped web's three isolation
edges fail for the *right* reason — curl exit 7 (ENETUNREACH), not a timeout or a
DNS artifact — against web-a, the mgmt adapter, and a public IP literal.

WHY: ``networking.py`` stresses one uplink switch (multi-``Network`` L2) plus one
air-gapped switch; nothing in the corpus certifies the **mgmt** tier as a
reachability boundary, nor the *directional* egress-isolation across all three
tiers at once. The mgmt host adapter is exactly the kind of seam a driver wires
subtly wrong — present but unreachable, or reachable from a guest that shares no
switch with it. Two subtleties this plan pins down so the green run can't lie.
(1) The mgmt adapter is a *host* IP, so any guest with a NAT egress path reaches
it incidentally — the host is that guest's egress next-hop, so a bare ping of
``.2`` succeeds via MASQUERADE even if the mgmt adapter were mis-homed onto the
wrong L2. The positive edge therefore asserts the kernel selects the **c1 leg**
(``ip route get`` ``src``), and the universal *isolation* proof is the air-gapped
guest (no route at all), not the NAT one. (2) A by-*name* egress-isolation probe
fails on DNS-resolution alone and would hide an air-gap *leak* that handed a guest
a route but no resolver — so routing isolation is asserted by **IP literal** and
by discriminating curl's exit code, while a positive ``web-b``-serves-locally
control rules out a broken-curl vacuous pass. A static guest on a NAT switch
deriving its gateway from the sidecar (rather than DHCP) is a further
under-exercised path. The matrix is adversarial: every reachable edge has a
mirror-image edge that MUST fail, and each failure is checked for the right cause,
so a driver that over-connects (a leaky air-gap, a mgmt adapter on the wrong L2, a
default-route storm) fails as loudly as one that under-connects.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/switch_isolation.py
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
from testrange.devices.network import DHCPAddr, NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

_WEB_A_IP = "10.50.0.100"
_WEB_B_IP = "10.51.0.100"
_CLIENT_B_IP = "10.51.0.101"
_CLIENT_C_IP = "10.52.0.100"
_A1_SIDECAR_IP = "10.50.0.1"  # sw-a sidecar (SIDECAR_OFFSET) = the client's only gateway
_MGMT_IP = "10.52.0.2"  # sw-c host adapter (MGMT_OFFSET); no service — reached by ping over c1
_ROUTE_PROBE_IP = "1.1.1.1"  # public IP literal: probes ROUTING egress without involving DNS
# deb.debian.org is the canonical NAT-egress probe — a stable, always-present index
# file reached by NAME, so a positive result exercises the sidecar resolver AND the
# NAT path together. (Routing isolation is asserted separately, by IP, via _ROUTE_PROBE_IP.)
_EGRESS_PROBE = (
    "curl -sf --max-time 15 -o /dev/null http://deb.debian.org/debian/dists/stable/Release"
)


def _web_image(content: str) -> CloudInitBuilder:
    # NativeCommunicator agent auto-provisioned per backend (CORE-90); only the
    # real app deps (nginx/curl) are declared here.
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
        packages=[Apt("nginx"), Apt("curl")],
        post_install_commands=(
            f"sh -c 'echo {content} > /var/www/html/index.html'",
            "systemctl enable --now nginx",
        ),
    )


PLAN = Plan(
    "switch_isolation",
    Hypervisor(
        build_switch=Switch(
            "build",
            Network("build-net"),
            cidr="10.97.99.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "sw-a",
                Network("a1"),
                cidr="10.50.0.0/24",
                uplink="egress",
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
            Switch(
                "sw-b",
                Network("b1"),
                cidr="10.51.0.0/24",
            ),
            Switch(
                "sw-c",
                Network("c1"),
                cidr="10.52.0.0/24",
                mgmt=True,
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="web-a",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("a1", addr=StaticAddr(_WEB_A_IP)),
                    ],
                ),
                builder=_web_image("WEB-A-OK"),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="web-b",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("b1", addr=StaticAddr(_WEB_B_IP)),
                    ],
                ),
                builder=_web_image("WEB-B-OK"),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="client",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("a1", addr=DHCPAddr()),
                        NetworkIface("b1", addr=StaticAddr(_CLIENT_B_IP)),
                        NetworkIface("c1", addr=StaticAddr(_CLIENT_C_IP)),
                    ],
                ),
                # NativeCommunicator agent auto-provisioned per backend (CORE-90);
                # the PosixCred is for ESXi VMware Tools guest-ops (CORE-60).
                builder=CloudInitBuilder(
                    base=CacheEntry("debian-13"),
                    credentials=[PosixCred("admin", password="testrange", admin=True)],
                    packages=[Apt("curl")],
                ),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def client_default_route_via_a1_sidecar(orch: OrchestratorHandle) -> None:
    # Exactly one default route AND it is the sw-a sidecar: a bare count is
    # necessary-but-not-sufficient (a single wrong route satisfies it too), so pin
    # the next-hop. sw-b/sw-c advertise no gateway, so the triple-homed client's
    # only egress is the sw-a NAT sidecar at .1 — a second gateway here is a bug.
    out = orch.vms["client"].communicator.execute(["ip", "-4", "route", "show", "default"])
    routes = [ln for ln in out.stdout.decode().splitlines() if ln.strip()]
    assert len(routes) == 1, f"triple-homed client must have one default route, got {routes!r}"
    assert f"via {_A1_SIDECAR_IP}" in routes[0], (
        f"client's default route is not via the sw-a sidecar {_A1_SIDECAR_IP}: {routes[0]!r}"
    )


def client_reaches_web_a_over_uplinked_switch_dns(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "http://web-a.a1/"], timeout=20.0
    )
    assert r.ok and b"WEB-A-OK" in r.stdout, f"client could not reach web-a over sw-a/DNS: {r}"


def client_reaches_web_b_over_airgapped_switch(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_WEB_B_IP}/"], timeout=20.0
    )
    assert r.ok and b"WEB-B-OK" in r.stdout, f"client could not reach web-b over sw-b: {r}"


def client_reaches_mgmt_over_c1_leg(orch: OrchestratorHandle) -> None:
    # The mgmt adapter is a HOST IP and the host is the client's NAT next-hop, so a
    # bare ping of .2 is over-determined — it would also succeed via the a1 MASQUERADE
    # path even if the c1 leg were mis-wired. Pin the path first: `ip route get` must
    # select the c1 leg (src == the client's c1 static), proving .2 is reached over
    # sw-c's L2, not incidentally via egress. Then ping confirms that path is live.
    g = orch.vms["client"].communicator
    route = g.execute(["ip", "route", "get", _MGMT_IP]).stdout.decode()
    assert f"src {_CLIENT_C_IP}" in route, (
        f"client reaches mgmt {_MGMT_IP} off its c1 leg (expected src {_CLIENT_C_IP}); "
        f"route is {route!r} — mgmt adapter not on the client's sw-c L2"
    )
    r = g.execute(["ping", "-c", "1", "-W", "3", _MGMT_IP], timeout=15.0)
    assert r.ok, f"client could not reach the mgmt host adapter {_MGMT_IP} over sw-c: {r}"


def client_reaches_internet_through_uplink(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(["sh", "-c", _EGRESS_PROBE], timeout=25.0)
    assert r.ok, f"client could not reach the internet through sw-a's NAT uplink: {r}"


def web_a_reaches_internet_through_uplink(orch: OrchestratorHandle) -> None:
    # web-a is static, not DHCP — its default route/DNS derive from the sidecar.
    r = orch.vms["web-a"].communicator.execute(["sh", "-c", _EGRESS_PROBE], timeout=25.0)
    assert r.ok, f"static web-a did not egress via its sidecar-derived gateway: {r}"


def web_b_serves_and_curls_locally(orch: OrchestratorHandle) -> None:
    # Positive control for web-b's isolation edges below: proves web-b's OWN curl
    # works and its nginx serves, so a failed curl in the negative tests is real
    # isolation — not a broken/missing curl binary passing the assertion vacuously.
    r = orch.vms["web-b"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "http://127.0.0.1/"], timeout=20.0
    )
    assert r.ok and b"WEB-B-OK" in r.stdout, f"web-b's own curl/nginx is not working: {r}"


def web_b_cannot_reach_web_a(orch: OrchestratorHandle) -> None:
    # By IP (the air-gapped segment has no resolver anyway): a pure L3 routing test.
    # Discriminate the failure — curl exit 7 is a connect failure (ENETUNREACH),
    # which excludes a vacuous pass from a timeout (28) or a leaked-but-slow path,
    # and a leak that let web-b connect would exit 0 and fail this assertion.
    r = orch.vms["web-b"].communicator.execute(
        ["curl", "-s", "--max-time", "5", "-o", "/dev/null", f"http://{_WEB_A_IP}/"], timeout=15.0
    )
    assert r.exit_code == 7, (
        f"web-b must fail to ROUTE to web-a {_WEB_A_IP} (curl exit 7 / unreachable); got {r}"
    )


def web_b_cannot_reach_mgmt(orch: OrchestratorHandle) -> None:
    # The air-gapped guest, not the NAT one (web-a reaches .2 incidentally via its
    # egress next-hop). web-b has no default route and .2 is off its subnet, so the
    # kernel rejects the ping with ENETUNREACH — a true "no path to the mgmt net".
    r = orch.vms["web-b"].communicator.execute(
        ["ping", "-c", "1", "-W", "3", _MGMT_IP], timeout=15.0
    )
    combined = (r.stdout + r.stderr).decode("utf-8", "replace").lower()
    assert not r.ok and "unreachable" in combined, (
        f"web-b must fail to reach the mgmt adapter {_MGMT_IP} (network unreachable); got {r}"
    )


def web_b_cannot_route_to_internet(orch: OrchestratorHandle) -> None:
    # IP literal, not a name: probes pure ROUTING isolation. A by-name probe fails on
    # DNS-resolution alone and would hide an air-gap leak that handed web-b a route
    # but no resolver; routing to a public IP fails iff web-b genuinely has no egress.
    r = orch.vms["web-b"].communicator.execute(
        ["curl", "-s", "--max-time", "5", "-o", "/dev/null", f"http://{_ROUTE_PROBE_IP}/"],
        timeout=15.0,
    )
    assert r.exit_code == 7, (
        f"web-b must fail to ROUTE to the internet {_ROUTE_PROBE_IP} (curl exit 7 / unreachable); "
        f"got {r}"
    )


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    client_default_route_via_a1_sidecar,
    client_reaches_web_a_over_uplinked_switch_dns,
    client_reaches_web_b_over_airgapped_switch,
    client_reaches_mgmt_over_c1_leg,
    client_reaches_internet_through_uplink,
    web_a_reaches_internet_through_uplink,
    web_b_serves_and_curls_locally,
    web_b_cannot_reach_web_a,
    web_b_cannot_reach_mgmt,
    web_b_cannot_route_to_internet,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
