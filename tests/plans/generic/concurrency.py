"""generic/concurrency: parallel bring-up and teardown of independent VMs.

WHAT: a fan-out of four structurally-identical but independent guests, each on
its own DHCP NIC and reached over the native agent. The tests assert every node
came up reachable and that each has its own distinct identity (hostname + lease)
— i.e. the parallel bring-up did not cross wires between VMs.

WHY: this plan exists to be run under ``--jobs N``. The orchestrator's
concurrent path is where shared-state races live: a driver resource map keyed by
the wrong VM, a build lock that serialises too little or too much, a teardown
that frees another worker's volume. Four independent VMs with no ordering
between them maximise the chance of surfacing a cross-VM race, and the teardown
after a green sweep exercises concurrent cleanup completeness.

Portable — bind a backend at run time and add ``--jobs`` to parallelise::

    testrange run --profile <name> --jobs 4 tests/plans/generic/concurrency.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import NativeCommunicator
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.packages import Apt
from testrange.vms import VMRecipe, VMSpec

_NODES = ("node-1", "node-2", "node-3", "node-4")


def _node(name: str) -> VMRecipe:
    return VMRecipe(
        spec=VMSpec(
            name=name,
            devices=[
                CPU(1),
                Memory(512),
                OSDrive("pool1", 8),
                NetworkIface("lab-net", addr=DHCPAddr()),
            ],
        ),
        builder=CloudInitBuilder(
            base=CacheEntry("debian-13"),
            packages=[Apt("qemu-guest-agent")],
            post_install_commands=("systemctl enable --now qemu-guest-agent",),
        ),
        communicator=NativeCommunicator(),
    )


PLAN = Plan(
    "concurrency",
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
                "lab",
                Network("lab-net"),
                cidr="10.40.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("pool1", 64)],
        vms=[_node(name) for name in _NODES],
    ),
)


def every_node_came_up_reachable(orch: OrchestratorHandle) -> None:
    for name in _NODES:
        assert orch.vms[name].communicator.execute(["true"]).ok, (
            f"{name} unreachable after bring-up"
        )


def every_node_reports_its_own_hostname(orch: OrchestratorHandle) -> None:
    for name in _NODES:
        r = orch.vms[name].communicator.execute(["hostname"])
        assert r.stdout.strip() == name.encode(), f"{name} reports wrong hostname: {r.stdout!r}"


def every_node_has_a_distinct_lease(orch: OrchestratorHandle) -> None:
    leases: dict[str, str] = {}
    for name in _NODES:
        out = orch.vms[name].communicator.execute(["ip", "-o", "-4", "addr"]).stdout.decode()
        leased = [tok for tok in out.split() if tok.startswith("10.40.0.")]
        assert leased, f"{name} got no lab-net lease: {out!r}"
        leases[name] = leased[0]
    assert len(set(leases.values())) == len(_NODES), f"duplicate leases across nodes: {leases!r}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    every_node_came_up_reachable,
    every_node_reports_its_own_hostname,
    every_node_has_a_distinct_lease,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
