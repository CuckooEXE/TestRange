"""libvirt_node: stand up a nested libvirt/KVM host as a libvirt guest, then leak it.

The certification vehicle for the REMOTE libvirt driver path (BACKEND-15): a
``GuestHypervisor.libvirt`` guest with an *empty* inner topology — the nested
machinery (ADR-0021) installs the qemu/libvirt stack, gates on libvirtd
readiness, and proves the inner ``qemu+ssh`` bind, but every actual range is
brought up later by pointing a ``libvirt-nested`` profile at the leaked node and
running ``tests/plans/generic/*`` + ``tests/plans/libvirt/*`` against it.

Why each knob is load-bearing:

- ``CPU(nested=True)`` — exposes vmx/svm so the node can run KVM guests at all.
- static ``.100`` on a ``mgmt=True`` NAT switch — the L0 host sits at ``.2`` on
  the node's segment and reaches the static address for ``qemu+ssh``; the NAT
  sidecar at ``.1`` is the node's own egress (chained NAT, ADR-0016).
- the baked ``tr-egress`` libvirt network — corpus plans bind their build
  switches to the logical ``egress`` uplink, which the ``libvirt-nested``
  profile maps to a NAT network that must already exist *on the node*
  (uplinks are out-of-band by design, ADR-0016). 192.168.210.0/24 avoids the
  L0 ``tr-egress`` (192.168.199.0/24) and every corpus CIDR.
- ``admin`` carries a deterministic key (seeded from the comment), so the
  profile's ``keyfile`` can be re-materialized at any time — see
  ``tools/standup/README.md``.
- resource names are unique (not ``build``/``lab``/``pool1``) so the leaked
  run's backend names never collide with same-day runs (run_id[:8] scoping).

Usage (leaks on success — tear down later with ``testrange cleanup <run-id>``)::

    testrange run --profile libvirt-local tools/standup/libvirt_node.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from testrange import Hypervisor, OrchestratorHandle, Plan, run_tests
from testrange.credentials import PosixCred
from testrange.devices import CPU, Memory, OSDrive, StoragePool
from testrange.devices.network import NetworkIface, StaticAddr
from testrange.networks import Network, Sidecar, Switch
from testrange.utils import SSHKey
from testrange.vms import GuestHypervisor, VMSpec

NODE_ADDR = "10.66.0.100"

_ADMIN = PosixCred(
    "admin", ssh_key=SSHKey.generate(comment="testrange-standup-libvirt"), admin=True
)

PLAN = Plan(
    "libvirt-node",
    Hypervisor(
        build_switch=Switch(
            "nodebuild",
            Network("nodebuild-net"),
            cidr="10.97.66.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        ),
        networks=[
            Switch(
                "nodemgmt",
                Network("nodemgmt-net"),
                cidr="10.66.0.0/24",
                uplink="egress",
                mgmt=True,
                sidecar=Sidecar(dhcp=True, dns=True, nat=True),
            ),
        ],
        pools=[StoragePool("nodepool", 220)],
        vms=[
            GuestHypervisor.libvirt(
                spec=VMSpec(
                    name="node",
                    devices=[
                        CPU(6, nested=True),
                        Memory(10240),
                        OSDrive("nodepool", 200),
                        NetworkIface("nodemgmt-net", addr=StaticAddr(f"{NODE_ADDR}/24")),
                    ],
                ),
                admin=_ADMIN,
                post_install_commands=(
                    # The corpus' logical `egress` uplink, realized on the node:
                    # a NAT+DHCP libvirt network mirroring the L0 tr-egress
                    # (string-form uplinks expect DHCP behind the bridge).
                    # libvirtd is already enabled by the .libvirt() front door's
                    # baked bring-up, which runs before these lines.
                    "cat > /tmp/tr-egress.xml <<'EOF'",
                    "<network>",
                    "  <name>tr-egress</name>",
                    "  <forward mode='nat'/>",
                    "  <bridge name='trbr0' stp='on'/>",
                    "  <ip address='192.168.210.1' netmask='255.255.255.0'>",
                    "    <dhcp>",
                    "      <range start='192.168.210.10' end='192.168.210.99'/>",
                    "    </dhcp>",
                    "  </ip>",
                    "</network>",
                    "EOF",
                    "virsh net-define /tmp/tr-egress.xml",
                    "virsh net-autostart tr-egress",
                ),
            ),
        ],
    ),
)


def node_runs_libvirtd(orch: OrchestratorHandle) -> None:
    r = orch.vms["node"].communicator.execute(["systemctl", "is-active", "libvirtd"])
    assert r.stdout.strip() == b"active", f"libvirtd not active on the node: {r}"


def node_cpu_exposes_virtualization(orch: OrchestratorHandle) -> None:
    r = orch.vms["node"].communicator.execute(["grep", "-cE", "vmx|svm", "/proc/cpuinfo"])
    assert r.ok and int(r.stdout.strip()) > 0, f"vmx/svm not exposed to the node: {r}"


def node_egress_network_is_defined(orch: OrchestratorHandle) -> None:
    r = orch.vms["node"].communicator.execute(["virsh", "net-list", "--all", "--name"])
    assert b"tr-egress" in r.stdout, f"tr-egress network missing on the node: {r}"


def leak_node_for_certification(orch: OrchestratorHandle) -> None:
    # Retain the running node as the remote-libvirt certification target. Point
    # the `libvirt-nested` profile at NODE_ADDR; tear down later with
    # `testrange cleanup <run-id>`.
    orch.leak()


TESTS: list[Callable[[OrchestratorHandle], None]] = [
    node_runs_libvirtd,
    node_cpu_exposes_virtualization,
    node_egress_network_is_defined,
    leak_node_for_certification,
]


if __name__ == "__main__":
    results = run_tests(TESTS, PLAN)
    sys.exit(0 if all(r.passed for r in results) else 1)
