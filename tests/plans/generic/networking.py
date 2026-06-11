"""generic/networking: the L2/L3 contract and the air-gap reachability matrix.

WHAT: three guests across two switches. ``pub-sw`` carries two Networks
(``pub-a``/``pub-b``) on one L2 with a NAT+DNS sidecar; ``priv-sw`` is an
air-gapped switch with no sidecar. ``client`` is multi-homed (static on the
air-gapped segment, DHCP on ``pub-a``, an unmanaged NIC on ``pub-b``);
``private-web`` sits alone on the air-gapped segment; ``public-web`` takes a DHCP
lease on ``pub-b``. The tests assert the full reachability matrix: internal L2
reach across the air-gap both ways, cross-label name resolution and reach over a
single shared switch, exactly one default route on the multi-homed guest, a DHCP
lease inside the declared pool, NAT egress for the public guest, and NO egress
for the air-gapped one.

WHY: this is where a driver's switch/sidecar wiring is most likely to be subtly
wrong — a second Network on a switch that does not actually share L2, a DHCP pool
that leaks outside its bounds, a default-route storm from multiple NICs, or an
"air-gapped" segment that quietly reaches the internet. The matrix is adversarial
on purpose: it asserts both reachability AND isolation.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/networking.py
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

_PRIVATE_WEB_IP = "10.20.0.100"
_CLIENT_PRIVATE_IP = "10.20.0.101"
_PUB_DHCP_LO = 10
_PUB_DHCP_HI = 99


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
    "networking",
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
                "pub-sw",
                Network("pub-a"),
                Network("pub-b"),
                cidr="10.30.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
            Switch(
                "priv-sw",
                Network("priv-net"),
                cidr="10.20.0.0/24",
            ),
        ],
        pools=[StoragePool("pool1", 32)],
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="client",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("priv-net", addr=StaticAddr(_CLIENT_PRIVATE_IP)),
                        NetworkIface("pub-a", addr=DHCPAddr()),
                        NetworkIface("pub-b"),
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
            VMRecipe(
                spec=VMSpec(
                    name="private-web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("priv-net", addr=StaticAddr(_PRIVATE_WEB_IP)),
                    ],
                ),
                builder=_web_image("air-gapped"),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="public-web",
                    devices=[
                        CPU(1),
                        Memory(512),
                        OSDrive("pool1", 8),
                        NetworkIface("pub-b", addr=DHCPAddr()),
                    ],
                ),
                builder=_web_image("internet-connected"),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


def client_dhcp_lease_in_pool(orch: OrchestratorHandle) -> None:
    out = orch.vms["client"].communicator.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
    leased = [tok for tok in out.split() if tok.startswith("10.30.0.")]
    assert leased, f"DHCP NIC got no 10.30.0.x lease: {out!r}"
    octet = int(leased[0].split("/")[0].rsplit(".", 1)[1])
    assert _PUB_DHCP_LO <= octet <= _PUB_DHCP_HI, f"lease {leased[0]} outside the DHCP pool"


def client_has_exactly_one_default_route(orch: OrchestratorHandle) -> None:
    out = orch.vms["client"].communicator.execute(["ip", "-4", "route", "show", "default"])
    routes = [ln for ln in out.stdout.decode().splitlines() if ln.strip()]
    assert len(routes) == 1, f"expected exactly one default route, got {routes!r}"


def client_reaches_private_web_internally(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_PRIVATE_WEB_IP}/"], timeout=20.0
    )
    assert r.ok and b"air-gapped" in r.stdout, f"client could not reach private-web: {r}"


def client_reaches_public_web_across_labels_via_dns(orch: OrchestratorHandle) -> None:
    r = orch.vms["client"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "http://public-web.pub-b/"], timeout=20.0
    )
    assert r.ok and b"internet-connected" in r.stdout, f"cross-label/DNS reach failed: {r}"


def private_web_reaches_client_internally(orch: OrchestratorHandle) -> None:
    r = orch.vms["private-web"].communicator.execute(
        ["ping", "-c", "1", "-W", "2", _CLIENT_PRIVATE_IP], timeout=15.0
    )
    assert r.ok, f"air-gapped segment lost internal L2 reachability to the client: {r}"


def private_web_cannot_reach_internet(orch: OrchestratorHandle) -> None:
    r = orch.vms["private-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "5", "-o", "/dev/null", "https://google.com/"], timeout=15.0
    )
    assert not r.ok, "air-gapped private-web reached the internet"


def public_web_reaches_internet_through_nat(orch: OrchestratorHandle) -> None:
    r = orch.vms["public-web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", "-o", "/dev/null", "https://google.com/"], timeout=20.0
    )
    assert r.ok, f"public-web could not reach the internet through NAT: {r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    client_dhcp_lease_in_pool,
    client_has_exactly_one_default_route,
    client_reaches_private_web_internally,
    client_reaches_public_web_across_labels_via_dns,
    private_web_reaches_client_internally,
    private_web_cannot_reach_internet,
    public_web_reaches_internet_through_nat,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
