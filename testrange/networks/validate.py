"""Plan-level cross-VM/Switch addressing validation.

Single-NIC checks (parseable IPv4) live in ``NetworkIface.__post_init__``.
Anything that needs the full plan in hand — subnet membership against
the owning Switch, reserved-slot collisions (sidecar at ``.1``, mgmt at
``.2``), DHCP-pool collision, duplicates across VMs — lives here so a
user sees every problem in one pass instead of fix-one-retry-find-next.

The DHCP pool bounds and reserved offsets come from
:mod:`testrange.networks._addressing_consts` so the validator and the
sidecar's dnsmasq config can never drift apart.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from testrange.networks._addressing_consts import (
    DHCP_RANGE_HI,
    DHCP_RANGE_LO,
    MGMT_OFFSET,
    SIDECAR_OFFSET,
)
from testrange.networks.base import Switch
from testrange.vms.recipe import VMRecipe


def validate_addressing(switches: Iterable[Switch], vms: Iterable[VMRecipe]) -> None:
    """Validate every NIC against the plan's Switch shape.

    Accumulates all issues and raises one ``ValueError`` containing every
    problem so the user can fix them in one pass.
    """
    switch_for: dict[str, Switch] = {}
    for sw in switches:
        for net in sw.networks:
            if net.name in switch_for:
                continue
            switch_for[net.name] = sw

    vms_list = list(vms)
    problems: list[str] = []
    seen_per_net: dict[str, dict[str, str]] = {}

    for vm in vms_list:
        for idx, nic in enumerate(vm.spec.nics):
            origin = f"VM {vm.name!r} NIC {idx} ({nic.network!r})"
            switch_opt = switch_for.get(nic.network)
            if switch_opt is None:
                problems.append(f"{origin}: references unknown network {nic.network!r}")
                continue
            switch = switch_opt

            if nic.ipv4 is None:
                if not switch.dhcp:
                    problems.append(
                        f"{origin}: nic_no_address — switch {switch.name!r} has "
                        f"dhcp=False and the NIC declares no static ipv4; this "
                        f"NIC would never get an address. Set ipv4= on the NIC "
                        f"or set dhcp=True on the switch."
                    )
                continue

            origin = f"{origin}={nic.ipv4}"
            subnet = switch.network
            try:
                addr = ipaddress.IPv4Address(nic.ipv4)
            except ValueError as e:  # pragma: no cover (caught at NIC level)
                problems.append(f"{origin}: {e}")
                continue
            if addr not in subnet:
                problems.append(
                    f"{origin}: address not in subnet {subnet!s} (switch {switch.name!r})"
                )
                continue
            if addr == subnet.network_address:
                problems.append(f"{origin}: address is the subnet's network address")
                continue
            if addr == subnet.broadcast_address:
                problems.append(f"{origin}: address is the subnet's broadcast address")
                continue
            if switch.needs_sidecar:
                sidecar = subnet.network_address + SIDECAR_OFFSET
                if addr == sidecar:
                    problems.append(
                        f"{origin}: address collides with sidecar slot {sidecar!s}"
                    )
                    continue
            if switch.mgmt:
                mgmt = subnet.network_address + MGMT_OFFSET
                if addr == mgmt:
                    problems.append(
                        f"{origin}: address collides with mgmt slot {mgmt!s}"
                    )
                    continue
            if switch.dhcp:
                lo = subnet.network_address + DHCP_RANGE_LO
                hi = subnet.network_address + DHCP_RANGE_HI
                if lo <= addr <= hi:
                    problems.append(
                        f"{origin}: address falls inside the DHCP pool "
                        f"({lo!s}-{hi!s}); pick something in "
                        f"{subnet.network_address + 100!s}-"
                        f"{subnet.network_address + 254!s}"
                    )
                    continue

            seen = seen_per_net.setdefault(nic.network, {})
            prior = seen.get(nic.ipv4)
            if prior is not None:
                problems.append(f"{origin}: duplicate — address already used by {prior}")
                continue
            seen[nic.ipv4] = origin

    if problems:
        joined = "\n  - ".join(problems)
        raise ValueError(f"plan has {len(problems)} addressing problem(s):\n  - {joined}")


__all__ = ["validate_addressing"]
