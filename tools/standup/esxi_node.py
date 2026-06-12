"""esxi_node: stand up a TestRange-BUILT nested ESXi node on libvirt, then leak it.

The certification vehicle for the managed-ESXi path (ESXI-34): an ESXi guest
whose ``ESXiKickstartBuilder`` performs the unattended install
(installer-origin, serial build-result; the v0 ``GuestHypervisor.esxi``
bring-up, inlined — the nested machinery is removed in 2.0, ADR-0030), and the
corpus is then run by pointing an ``esxi-nested`` profile at the leaked node
(``tests/plans/generic/*`` Native plans + ``tests/plans/esxi/*``; the SSH
generic plans are not applicable on ESXi, ESXI-30).

Why each knob is load-bearing (ADR-0021 ORCH-32, ADR-0026):

- ``firmware="bios"`` + ``LibvirtOSDrive(bus="sata")`` + e1000e NICs — ESXi has
  no virtio drivers, and the installer CD must enumerate on IDE, which the
  libvirt driver provides on a BIOS machine (the nested-ESXi gotcha set).
- ``CPU(nested=True)`` — vmx exposure so the node can power on its own VMs.
- two e1000e NICs on the lab switch: the first becomes vmnic0/vmk0 (mgmt, DHCP
  lease the orchestrator discovers), the second stays unconfigured and becomes
  the free pNIC (vmnic1) the ESXi driver enslaves for its NAT-egress uplink
  vSwitch — corpus sidecars DHCP+NAT through the lab sidecar (chained NAT).
- the first run boot performs the sentinel-guarded vmk0 MAC-follow reboot
  (ESXI-18), so lease discovery spans TWO boots — run with a generous
  ``--lease-timeout`` (and ``--build-timeout 1800``: nested install is slow).
- no license: a fresh install runs the fully-functional 60-day evaluation, the
  same mode the unmanaged cert node used. Export ``TESTRANGE_ESXI_LICENSE`` to
  bake one instead.

Usage (leaks on success — tear down later with ``testrange cleanup <run-id>``)::

    testrange run --profile libvirt-local --build-timeout 1800 \\
        --lease-timeout 900 tools/standup/esxi_node.py
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.builders import ESXiKickstartBuilder
from testrange.cache import CacheEntry
from testrange.communicators import SSHCommunicator
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, StoragePool
from testrange.devices.disk.libvirt import LibvirtOSDrive
from testrange.devices.network import DHCPAddr
from testrange.devices.network.libvirt import LibvirtNetworkIface
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import EcdsaKey
from testrange.vms import VMRecipe, VMSpec

# ESXi 8 sshd is FIPS-constrained and silently rejects Ed25519 — ECDSA keypair
# (deterministic, re-materializable from the comment). The password is what the
# esxi-nested profile and the pyVmomi bind use.
_ROOT = PosixCred(
    "root",
    password="TestRangeNested1!",
    ssh_key=EcdsaKey.generate(comment="testrange-standup-esxi"),
)

hyp = Hypervisor(
    build_switch=Switch(
        "esxbuild",
        Network("esxbuild-net"),
        cidr="10.97.67.0/24",
        uplink="egress",
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

hyp.add_pool(StoragePool("esxpool", 140))

hyp.add_switch(
    Switch(
        "esxlab",
        Network("esxlab-net"),
        cidr="10.67.0.0/24",
        uplink="egress",
        mgmt=True,
        sidecar=Sidecar(dhcp=True, dns=True, nat=True),
    )
)

hyp.add_vm(
    VMRecipe(
        spec=VMSpec(
            name="esxi1",
            firmware="bios",
            devices=[
                CPU(4, nested=True),
                Memory(10240),
                LibvirtOSDrive(hyp.pools["esxpool"], 120, bus="sata"),
                LibvirtNetworkIface(hyp.networks["esxlab-net"], model="e1000e", addr=DHCPAddr()),
                LibvirtNetworkIface(hyp.networks["esxlab-net"], model="e1000e", addr=None),
            ],
        ),
        # The v0 GuestHypervisor.esxi pairing, inlined: SSH is the transport, so
        # the builder bakes the root key + sshd (enable_ssh defaults True).
        builder=ESXiKickstartBuilder(
            installer_iso=CacheEntry("esxi-installer"),
            credentials=[_ROOT],
            license=os.environ.get("TESTRANGE_ESXI_LICENSE") or None,
        ),
        communicator=SSHCommunicator("root"),
    )
)

PLAN = Plan("esxi-node", hyp)


def node_answers_over_ssh(orch: OrchestratorHandle) -> None:
    r = orch.vms["esxi1"].communicator.execute(["esxcli", "system", "version", "get"])
    assert r.ok and b"8." in r.stdout, f"nested ESXi not answering over SSH: {r}"


def node_has_a_local_datastore(orch: OrchestratorHandle) -> None:
    # systemMediaSize=min must leave a VMFS datastore1 for the driver's pools.
    r = orch.vms["esxi1"].communicator.execute(["ls", "/vmfs/volumes/datastore1"])
    assert r.ok, f"datastore1 missing — install consumed the whole disk: {r}"


def node_has_a_free_uplink_pnic(orch: OrchestratorHandle) -> None:
    r = orch.vms["esxi1"].communicator.execute(["esxcli", "network", "nic", "list"])
    assert b"vmnic1" in r.stdout, f"second pNIC (driver egress uplink) missing: {r}"


def report_node_address(orch: OrchestratorHandle) -> None:
    com = orch.vms["esxi1"].communicator
    assert isinstance(com, SSHCommunicator) and com.host, "node address was not discovered"
    print(f"esxi-nested node reachable at {com.host}")


def leak_node_for_certification(orch: OrchestratorHandle) -> None:
    # Retain the node as the ESXi driver's certification target. Point the
    # `esxi-nested` profile at the address printed above; tear down later with
    # `testrange cleanup <run-id>`.
    orch.leak()


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    node_answers_over_ssh,
    node_has_a_local_datastore,
    node_has_a_free_uplink_pnic,
    report_node_address,
    leak_node_for_certification,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
