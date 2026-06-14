"""generic/preflight: the host-resource preflight gate (CORE-84).

WHAT: proves the bound backend can introspect its host's resource ceiling
(``driver.host_capacity()``) and that the shared resource gate
(``resource_findings``) accepts the plan that is *currently running* while
rejecting an impossible ask — a VM larger than the whole host — computed against
the **live** capacity the backend reported.

WHY: preflight is the only thing between a typo'd ``Memory(size_mb=...)`` and an
opaque create-time failure. The gate is only as good as the backend's ability to
report a usable ceiling; this asserts every backend reports one and honours the
same comparison, so the check never silently does nothing on a given backend.

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/preflight.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import CloudInitBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.preflight import resource_findings
from testrange.utils import SSHKey

_KEY = SSHKey.generate(comment="testrange-preflight")

hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
pool1 = hyp.add_pool(StoragePool("pool1", 16))
hyp.add_switch(
    Switch(
        "lab",
        Network("lab-net"),
        cidr="10.42.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
lab_net = hyp.networks["lab-net"]
hyp.vm(
    "probe",
    cpu=CPU(1),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(lab_net, DHCPAddr())],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", ssh_key=_KEY, admin=True)],
    ),
    communicator=SSHCommunicator("admin"),
)

PLAN = Plan("preflight", hyp)


def _oversized_plan(memory_mb: int) -> Plan:
    """A throwaway single-VM plan asking ``memory_mb`` — never realized, only fed
    to the resource gate alongside the live capacity."""
    big = Hypervisor()
    pool1 = big.add_pool(StoragePool("pool1", 16))
    big.add_switch(Switch("lab", Network("lab-net"), cidr="10.42.0.0/24"))
    big.vm(
        "toobig",
        cpu=CPU(1),
        memory=Memory(memory_mb),
        os_drive=OSDrive(pool1, 8),
        builder=CloudInitBuilder(base=CacheEntry("debian-13")),
        communicator=SSHCommunicator("admin"),
    )
    return Plan("preflight-oversized", big)


def host_capacity_reports_a_usable_ceiling(orch: OrchestratorHandle) -> None:
    cap = orch.driver.host_capacity()
    assert cap is not None, "backend reported no host capacity; the resource gate is dead here"
    assert cap.memory_mb is not None and cap.memory_mb > 0, f"bad memory ceiling: {cap}"
    assert cap.logical_cpus is not None and cap.logical_cpus > 0, f"bad cpu ceiling: {cap}"


def the_running_plan_fits_its_host(orch: OrchestratorHandle) -> None:
    # The plan is up, so it self-evidently fits — the gate must not false-positive.
    cap = orch.driver.host_capacity()
    assert cap is not None
    assert resource_findings(PLAN, cap) == (), "the running plan was flagged as too large"


def an_impossible_memory_ask_is_rejected(orch: OrchestratorHandle) -> None:
    # A VM asking for 1000x the host's RAM is the canonical impossible ask.
    cap = orch.driver.host_capacity()
    assert cap is not None and cap.memory_mb is not None
    findings = resource_findings(_oversized_plan(cap.memory_mb * 1000), cap)
    codes = {f.code for f in findings}
    assert "insufficient-memory" in codes, f"impossible memory ask not rejected: {findings}"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    host_capacity_reports_a_usable_ceiling,
    the_running_plan_fits_its_host,
    an_impossible_memory_ask_is_rejected,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
