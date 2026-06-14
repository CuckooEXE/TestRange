"""generic/lifecycle: VM lifecycle, power-state churn, disk growth, and bring-up order.

WHAT: drives a guest through repeated power cycles, proves a graceful shutdown
actually reaches the ``shutoff`` state, that on-disk state survives a
reboot, and that an oversized OS drive grows to its declared size on first boot.
A second NIC-less guest proves the native guest-agent path survives the same
churn with no networking at all. A ``db``/``web`` pair certifies the explicit
inter-node ordering edge (ADR-0030) **end-to-end**: ``web.needs(db)`` must place
``db`` in an earlier realize wave, and web's first test finds db's nginx already
serving — the edge proven against a live backend, not just in the graph model.

WHY: the orchestrator's power and readiness logic is where reconnect and
off-by-one bugs hide — a communicator that does not re-bind after ``start_vm``,
a ``shutdown_vm`` that returns before the guest is truly off, a rootfs that never
grows past the base image. Every backend must hold these invariants identically.
The ordering edge is the one 2.0 graph feature with no v0 equivalent: an
executor that quietly ignored explicit edges and realized every VM in one wave
would pass every other plan in the corpus, so this pair pins both the frozen
graph (the edge exists and orders the waves) and the live consequence (the
needed VM is up and serving before the dependent's tests run).

Portable — bind a backend at run time::

    testrange run --profile <name> tests/plans/generic/lifecycle.py
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

_DB_IP = "10.40.0.200"


def _native_image() -> CloudInitBuilder:
    # The NativeCommunicator's agent (qemu-guest-agent / open-vm-tools) is
    # auto-provisioned per backend by the driver (CORE-90) — not declared here.
    # The PosixCred is for ESXi VMware Tools guest-ops, which authenticate per
    # call (CORE-60); QGA backends ignore it.
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
    )


hyp = Hypervisor(
    build_switch=Switch(
        "build",
        Network("build-net"),
        cidr="10.97.99.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    ),
)
pool1 = hyp.add_pool(StoragePool("pool1", 64))
hyp.add_switch(
    Switch(
        "lab",
        Network("lab-net"),
        cidr="10.40.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)
lab_net = hyp.networks["lab-net"]
hyp.vm(
    "churn",
    cpu=CPU(2),
    memory=Memory(1024),
    os_drive=OSDrive(pool1, 16),
    nics=[NetworkIface(lab_net, DHCPAddr())],
    builder=_native_image(),
    communicator=NativeCommunicator(),
)
hyp.vm(
    "headless",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    builder=_native_image(),
    communicator=NativeCommunicator(),
)
db = hyp.vm(
    "db",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(lab_net, StaticAddr(_DB_IP))],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
        packages=[Apt("nginx")],
        post_install_commands=(
            "sh -c 'echo db-online > /var/www/html/index.html'",
            "systemctl enable --now nginx",
        ),
    ),
    communicator=NativeCommunicator(),
)
web = hyp.vm(
    "web",
    cpu=CPU(1),
    memory=Memory(512),
    os_drive=OSDrive(pool1, 8),
    nics=[NetworkIface(lab_net, DHCPAddr())],
    builder=CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
        packages=[Apt("curl")],
    ),
    communicator=NativeCommunicator(),
)
web.needs(db)

PLAN = Plan("lifecycle", hyp)


def web_first_test_finds_db_already_serving(orch: OrchestratorHandle) -> None:
    # First in TESTS on purpose: web.needs(db) promises db was realized (and
    # its boot-time-enabled nginx serving) before web's tests run — probe that
    # promise before anything else has had a chance to touch either guest.
    r = orch.vms["web"].communicator.execute(
        ["curl", "-sf", "--max-time", "10", f"http://{_DB_IP}/"], timeout=20.0
    )
    assert r.ok and b"db-online" in r.stdout, (
        f"web.needs(db): db must already be serving at web's first test: {r}"
    )


def ordering_edge_places_db_in_an_earlier_wave(orch: OrchestratorHandle) -> None:
    # The live probe above cannot discriminate ordering on its own (tests run
    # after full bring-up), so pin the contract in the frozen graph too: the
    # explicit edge must exist and must gate web's wave behind db's.
    deps = {n.name for n in PLAN.graph.dependencies_of("vm:web")}
    assert "vm:db" in deps, f"web.needs(db) edge missing from the frozen graph: {deps}"
    wave_of = {n.name: i for i, wave in enumerate(PLAN.graph.waves()) for n in wave}
    assert wave_of["vm:db"] < wave_of["vm:web"], (
        f"db must realize in an earlier wave than web: "
        f"db wave {wave_of['vm:db']}, web wave {wave_of['vm:web']}"
    )
    assert orch.vms["db"].communicator.execute(["true"]).ok, "db unreachable (positive control)"


def churn_survives_repeated_power_cycles(orch: OrchestratorHandle) -> None:
    vm = orch.vms["churn"]
    driver = orch.driver
    com = vm.communicator
    for i in range(3):
        driver.shutdown_vm(vm.backend_name, timeout=120.0)
        assert driver.get_vm_power_state(vm.backend_name) == "shutoff", f"cycle {i}: not shutoff"
        driver.start_vm(vm.backend_name)
        com.close()
        assert com.execute(["true"]).ok, f"cycle {i}: guest unreachable after start"


def reboot_persists_on_disk_state(orch: OrchestratorHandle) -> None:
    vm = orch.vms["churn"]
    driver = orch.driver
    com = vm.communicator
    # admin-writable path: ESXi guest-ops run as the declared admin, not root
    # (CORE-60); QGA roots can write here just as well (ESXI-38).
    com.write_file("/home/admin/persist", b"survives\n")
    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    driver.start_vm(vm.backend_name)
    com.close()
    assert com.read_file("/home/admin/persist") == b"survives\n", "on-disk state lost across reboot"


def oversized_os_drive_grew_on_first_boot(orch: OrchestratorHandle) -> None:
    r = orch.vms["churn"].communicator.execute(["df", "-BG", "--output=size", "/"])
    size_gb = int(r.stdout.decode().splitlines()[-1].strip().rstrip("G"))
    assert size_gb >= 14, f"rootfs did not grow to the 16G OSDrive: {size_gb}G"


def native_write_handles_payload_over_the_agent_cap(orch: OrchestratorHandle) -> None:
    # A payload larger than any single guest-agent write (PVE caps a write at
    # ~45 KB; QGA caps a single command too) must round-trip intact over the
    # native channel — exercises the driver's chunked write (REL-33).
    com = orch.vms["churn"].communicator
    blob = bytes(i % 256 for i in range(256 * 1024))  # 256 KiB, every byte value
    com.write_file("/home/admin/big.bin", blob)
    readback = com.read_file("/home/admin/big.bin")
    assert readback == blob, (
        f"large native write corrupted: wrote {len(blob)} bytes, read {len(readback)}"
    )


def headless_reachable_over_native_agent(orch: OrchestratorHandle) -> None:
    assert orch.vms["headless"].communicator.execute(["true"]).ok, "NIC-less guest unreachable"


def headless_has_no_ethernet(orch: OrchestratorHandle) -> None:
    r = orch.vms["headless"].communicator.execute(["ip", "-o", "-4", "addr"])
    addrs = [ln for ln in r.stdout.decode().splitlines() if " lo " not in ln]
    assert not addrs, f"NIC-less guest has an IPv4 address: {addrs!r}"


def headless_survives_power_cycle(orch: OrchestratorHandle) -> None:
    vm = orch.vms["headless"]
    driver = orch.driver
    com = vm.communicator
    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    assert driver.get_vm_power_state(vm.backend_name) == "shutoff", "headless did not power off"
    driver.start_vm(vm.backend_name)
    com.close()
    assert com.execute(["true"]).ok, "NIC-less guest unreachable after a power cycle"


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    web_first_test_finds_db_already_serving,
    ordering_edge_places_db_in_an_earlier_wave,
    churn_survives_repeated_power_cycles,
    reboot_persists_on_disk_state,
    oversized_os_drive_grew_on_first_boot,
    native_write_handles_payload_over_the_agent_cap,
    headless_reachable_over_native_agent,
    headless_has_no_ethernet,
    headless_survives_power_cycle,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
