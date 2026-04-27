"""Two PVE SDN zones, multiple vnets per zone — the explicit Switch form.

This example shows how to opt into the
:class:`~testrange.AbstractSwitch` layer when you need more than the
default single-zone setup that
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator` provides
out of the box.

Topology
========

::

    PVE node
      ├── SDN Zone "Lab" (simple, isolated — TestRange-managed)
      │     ├── vnet "Mgmt"      10.0.10.0/24  internet=True
      │     └── vnet "Internal"  10.0.11.0/24  internet=False
      │           └─── client    10.0.10.5  /  10.0.11.5
      │
      └── SDN Zone "Corp" (VLAN, bound to physical uplink eno1)
            └── vnet "Public"    10.42.0.0/24  internet=True (via uplink)
                  └── webpublic  10.42.0.5

Two switches:

1. **Lab** — a ``simple`` zone, just like the default ``tr`` zone the
   orchestrator creates on its own.  Demonstrates that you can name
   and own additional simple zones for organisational reasons (one
   zone per project, one per team, etc.) without losing the
   isolated-by-default behaviour.

2. **Corp** — a ``vlan`` zone bound to a physical NIC.  Demonstrates
   uplinks: traffic from any VM on a Corp-zone vnet egresses through
   ``eno1`` on the PVE node, hitting the physical network the host
   is wired into.

The single inner ``client`` VM is dual-homed (one NIC on each
``Lab`` vnet) and a sibling ``webpublic`` lives on ``Corp/Public``.

What this example demonstrates
==============================

1. Declaring multiple :class:`~testrange.Switch` instances and
   passing them to ``ProxmoxOrchestrator(switches=...)``.
2. Binding each :class:`~testrange.VirtualNetwork` to a specific
   switch via ``switch=`` — by instance reference here, by name
   string is also fine.
3. PVE SDN zone-type variation in one orchestrator (one ``simple``,
   one ``vlan``).
4. Uplink wiring (``uplinks=["eno1"]``) for VLAN-zone egress
   through a real physical NIC.
5. The orchestrator handling the two-stage lifecycle: switch zones
   come up first, then vnets land in their declared zones; on exit
   vnets tear down before zones do.

Prerequisites
=============

- A reachable PVE node with a physical NIC named ``eno1`` (adjust
  :data:`UPLINK_NIC` below if yours is named differently).
- A user / token with permission to create SDN zones and vnets.
- ``testrange[proxmox]`` installed.

This example does **not** boot any inner VMs — it's a focused
demonstration of the networking layer.  For a fuller end-to-end
nested run, see ``examples/nested_proxmox_public_private.py``.

Running
=======

::

    testrange run examples/proxmox_explicit_zones.py:gen_tests

What success looks like
=======================

The verify function pulls the orchestrator's started switches and
confirms PVE created two zones with the expected types.  No VMs
boot — fast smoke test.
"""

from __future__ import annotations

import os

from testrange import (
    Switch,
    Test,
    VirtualNetwork,
    run_tests,
)
from testrange.backends.proxmox import ProxmoxOrchestrator


# Adjust to a real physical NIC name on the PVE node when running.
UPLINK_NIC = os.environ.get("TESTRANGE_PROXMOX_UPLINK", "eno1")

# Required env: PVE host + auth.  See ``tests/test_proxmox_live.py``
# for the same env-var convention.
PROXMOX_HOST = os.environ.get("TESTRANGE_PROXMOX_HOST", "pve.example.com")
PROXMOX_USER = os.environ.get("TESTRANGE_PROXMOX_USER", "root@pam")
PROXMOX_PASSWORD = os.environ.get("TESTRANGE_PROXMOX_PASSWORD", "")


# Two distinct switches with different zone types.  Both are
# orchestrator-owned: created on __enter__, deleted on __exit__.
lab_switch = Switch(
    "Lab",
    switch_type="simple",
    # Simple zones don't take an uplink — leave empty.
)

corp_switch = Switch(
    "Corp",
    switch_type="vlan",
    uplinks=[UPLINK_NIC],
)


# Three vnets across the two switches.  ``switch=`` can take an
# instance reference (as below) OR a string matching the switch's
# logical name; both work.
mgmt_net = VirtualNetwork(
    "Mgmt",
    "10.0.10.0/24",
    internet=True,
    dhcp=True,
    switch=lab_switch,
)
internal_net = VirtualNetwork(
    "Internal",
    "10.0.11.0/24",
    internet=False,
    dhcp=False,
    switch=lab_switch,
)
public_net = VirtualNetwork(
    "Public",
    "10.42.0.0/24",
    internet=True,
    dhcp=True,
    switch=corp_switch,
)


def verify(orch: ProxmoxOrchestrator) -> None:
    """Confirm both zones came up on PVE with the expected types."""
    # Pull the started switches off the orchestrator and ask PVE
    # what zones exist.  Each declared Switch should be present
    # with its declared switch_type.
    client = orch._client
    pve_zones = {
        z["zone"]: z for z in client.cluster.sdn.zones.get()
    }

    for sw in orch._switches:
        zone_id = sw.backend_name()
        assert zone_id in pve_zones, (
            f"Switch {sw.name!r} (zone {zone_id!r}) not found on "
            f"PVE; have: {sorted(pve_zones)}"
        )
        actual_type = pve_zones[zone_id].get("type")
        assert actual_type == sw.switch_type, (
            f"Switch {sw.name!r}: expected type "
            f"{sw.switch_type!r}, got {actual_type!r}"
        )

    # And each vnet should land in its switch's zone.
    pve_vnets = {
        v["vnet"]: v for v in client.cluster.sdn.vnets.get()
    }
    for net in orch._networks:
        vnet_id = net.backend_name()
        assert vnet_id in pve_vnets, (
            f"VNet {net.name!r} ({vnet_id!r}) not found on PVE; "
            f"have: {sorted(pve_vnets)}"
        )
        # vnet records carry the zone they belong to.
        actual_zone = pve_vnets[vnet_id].get("zone")
        # Match the network's resolved zone (handles instance /
        # string-name / None reference shapes).
        expected_zone = net._resolve_zone(orch)
        assert actual_zone == expected_zone, (
            f"VNet {net.name!r}: expected zone "
            f"{expected_zone!r}, got {actual_zone!r}"
        )


def gen_tests() -> list[Test]:
    return [
        Test(
            ProxmoxOrchestrator(
                host=PROXMOX_HOST,
                user=PROXMOX_USER,
                password=PROXMOX_PASSWORD,
                # Both switches declared up-front so the
                # orchestrator owns their lifecycle.
                switches=[lab_switch, corp_switch],
                # Three vnets, distributed across the two switches.
                networks=[mgmt_net, internal_net, public_net],
                # No VMs — this example focuses on the networking
                # layer.  Add some if you want a fuller end-to-end.
                vms=[],
            ),
            verify,
            name="proxmox-explicit-zones",
        ),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(r.passed for r in run_tests(gen_tests())) else 1)
