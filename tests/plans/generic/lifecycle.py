"""generic/lifecycle: VM lifecycle, power-state churn, and first-boot disk growth.

WHAT: drives a guest through repeated power cycles, proves a graceful shutdown
actually reaches the ``shutoff`` state, that on-disk state survives a
reboot, and that an oversized OS drive grows to its declared size on first boot.
A second NIC-less guest proves the native guest-agent path survives the same
churn with no networking at all.

WHY: the orchestrator's power and readiness logic is where reconnect and
off-by-one bugs hide — a communicator that does not re-bind after ``start_vm``,
a ``shutdown_vm`` that returns before the guest is truly off, a rootfs that never
grows past the base image. Every backend must hold these invariants identically.

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
from testrange.devices.network import DHCPAddr, NetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.vms import VMRecipe, VMSpec


def _native_image() -> CloudInitBuilder:
    # The NativeCommunicator's agent (qemu-guest-agent / open-vm-tools) is
    # auto-provisioned per backend by the driver (CORE-90) — not declared here.
    # The PosixCred is for ESXi VMware Tools guest-ops, which authenticate per
    # call (CORE-60); QGA backends ignore it.
    return CloudInitBuilder(
        base=CacheEntry("debian-13"),
        credentials=[PosixCred("admin", password="testrange", admin=True)],
    )


PLAN = Plan(
    "lifecycle",
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
        vms=[
            VMRecipe(
                spec=VMSpec(
                    name="churn",
                    devices=[
                        CPU(2),
                        Memory(1024),
                        OSDrive("pool1", 16),
                        NetworkIface("lab-net", addr=DHCPAddr()),
                    ],
                ),
                builder=_native_image(),
                communicator=NativeCommunicator(),
            ),
            VMRecipe(
                spec=VMSpec(
                    name="headless",
                    devices=[CPU(1), Memory(512), OSDrive("pool1", 8)],
                ),
                builder=_native_image(),
                communicator=NativeCommunicator(),
            ),
        ],
    ),
)


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
    com.write_file("/root/persist", b"survives\n")
    driver.shutdown_vm(vm.backend_name, timeout=120.0)
    driver.start_vm(vm.backend_name)
    com.close()
    assert com.read_file("/root/persist") == b"survives\n", "on-disk state lost across reboot"


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
    com.write_file("/root/big.bin", blob)
    readback = com.read_file("/root/big.bin")
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
