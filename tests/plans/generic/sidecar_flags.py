"""generic/sidecar_flags: the dhcp-only sidecar tier — leases without router or resolver.

WHAT: one air-gapped switch whose sidecar serves *only* DHCP — no DNS, no NAT —
and two leased guests. Leases must land in the ``.10``-``.99`` pool, be distinct,
and come back identical from a fresh DISCOVER after a guest power cycle (client
lease caches are wiped first, so the re-offer is keyed on the stable MAC alone).
Because ``nat`` and ``dns`` are off, the lease must offer *no* default route and
*no* resolver — the sidecar must not advertise itself beyond what its flags
declare. A peer-to-peer ping is the positive control that the tier serves.

WHY: every other sidecar in the corpus is ``(dhcp, dns, nat)`` or
``(dhcp, dns)``, so the option suppression the renderer deliberately emits for
the bare-dhcp tier (empty ``dhcp-option=3``/``=6`` forms, DNS listener off) has
no certification, and neither does lease stability across a reboot. Sidecar
flags describe what the sidecar provides — this plan makes that sentence
executable.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/sidecar_flags.py
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec


def _peer(name: str) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                NetworkIface("flags-net", addr=DHCPAddr()),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            # The PosixCred is for ESXi VMware Tools guest-ops, which
            # authenticate per call (CORE-60); QGA backends ignore it. admin=True
            # grants the passwordless sudo the lease-cache wipe needs.
            credentials=[PosixCred("admin", password="testrange", admin=True)],
        ),
        communicator=NativeCommunicator(),
    )


PLAN = Plan(
    "sidecar-flags",
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
                "flags",
                Network("flags-net"),
                cidr="10.63.0.0/24",
                sidecar=Sidecar(dhcp=True),
            ),
        ],
        pools=[StoragePool("pool1", 24)],
        vms=[_peer("peer-1"), _peer("peer-2")],
    ),
)


def _leased_addr(orch: OrchestratorHandle, name: str) -> str:
    # Poll up to the framework's own lease_timeout_s default (120s): after a
    # mid-test power cycle the agent can answer before the NIC has re-leased,
    # and the run-phase lease gate only covers bring-up. Returns on the first
    # non-empty poll, so the happy path pays nothing.
    com = orch.vms[name].communicator
    for _ in range(60):
        out = com.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
        addrs = [ln.split()[3].split("/")[0] for ln in out.splitlines() if " lo " not in ln]
        if addrs:
            assert len(addrs) == 1, f"{name} has more than one IPv4 address: {addrs}"
            return addrs[0]
        time.sleep(2)
    raise AssertionError(f"{name} never acquired an IPv4 lease")


def leases_land_in_the_dhcp_pool(orch: OrchestratorHandle) -> None:
    for name in ("peer-1", "peer-2"):
        addr = _leased_addr(orch, name)
        octet = int(addr.rsplit(".", 1)[1])
        assert addr.startswith("10.63.0.") and 10 <= octet <= 99, (
            f"{name} leased {addr}, outside the .10-.99 pool"
        )


def leases_are_distinct(orch: OrchestratorHandle) -> None:
    assert _leased_addr(orch, "peer-1") != _leased_addr(orch, "peer-2"), "peers share a lease"


def fresh_discover_after_a_power_cycle_reoffers_the_same_lease(orch: OrchestratorHandle) -> None:
    # The contract: the driver's deterministic MAC (compose_mac, ADR-0006) plus
    # the sidecar dnsmasq's retained lease db re-offer the same address. Client
    # lease caches are wiped first so the post-cycle address can only come from
    # a fresh DISCOVER answered by the live sidecar keyed on the MAC — not from
    # the guest re-asserting a cached lease with the server dead or reshuffled.
    vm = orch.vms["peer-1"]
    com = vm.communicator
    before = _leased_addr(orch, "peer-1")
    r = com.execute(
        ["sudo", "-n", "sh", "-c", "rm -f /var/lib/dhcp/*.leases /run/systemd/netif/leases/*"]
    )
    assert r.ok, f"wiping the client lease cache failed: {r}"
    orch.driver.shutdown_vm(vm.backend_name, timeout=120.0)
    assert orch.driver.get_vm_power_state(vm.backend_name) == "shutoff", "peer-1 not shutoff"
    orch.driver.start_vm(vm.backend_name)
    com.close()
    after = _leased_addr(orch, "peer-1")
    assert after == before, f"lease changed across a power cycle: {before} -> {after}"


def no_default_route_is_offered(orch: OrchestratorHandle) -> None:
    for name in ("peer-1", "peer-2"):
        r = orch.vms[name].communicator.execute(["ip", "-4", "route", "show", "default"])
        assert r.ok, f"{name}: route probe itself failed: {r}"
        assert r.stdout.strip() == b"", (
            f"{name} got a default route from a nat=False sidecar: {r.stdout!r}"
        )


def no_resolver_is_offered(orch: OrchestratorHandle) -> None:
    for name in ("peer-1", "peer-2"):
        com = orch.vms[name].communicator
        r = com.execute(["resolvectl", "dns"])
        # r.ok + the Global header prove resolved answered — a dead resolver
        # frontend would otherwise make the absence check pass vacuously.
        assert r.ok and b"Global" in r.stdout, f"{name}: resolver probe failed: {r}"
        assert b"10.63.0.1" not in r.stdout, (
            f"{name} was offered the sidecar as resolver despite dns=False: {r.stdout!r}"
        )
        # Second observation channel: a dhclient/resolvconf-style stack writes
        # DHCP-offered DNS straight to /etc/resolv.conf, bypassing resolved.
        r = com.execute(["cat", "/etc/resolv.conf"])
        assert r.ok and b"10.63.0.1" not in r.stdout, (
            f"{name}: sidecar leaked into resolv.conf despite dns=False: {r.stdout!r}"
        )


def peers_reach_each_other_on_the_leased_segment(orch: OrchestratorHandle) -> None:
    target = _leased_addr(orch, "peer-2")
    r = orch.vms["peer-1"].communicator.execute(
        ["ping", "-c", "3", "-W", "5", target], timeout=30.0
    )
    assert r.ok, f"leased peers cannot reach each other (positive control): {r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    leases_land_in_the_dhcp_pool,
    leases_are_distinct,
    fresh_discover_after_a_power_cycle_reoffers_the_same_lease,
    no_default_route_is_offered,
    no_resolver_is_offered,
    peers_reach_each_other_on_the_leased_segment,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
