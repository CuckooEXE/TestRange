"""Plan-level cross-VM/network addressing validation.

Single-NIC checks (parseable IPv4) live in ``NetworkIface.__post_init__``.
Anything that needs the full plan in hand — subnet membership, gateway
collision, DHCP-pool collision, duplicates across VMs, DHCP-disabled-network
+ DHCP-NIC — lives here so a user sees every problem in one pass instead of
fix-one-retry-find-next.

The DHCP pool (``.100`` to ``.200``) mirrors the range the driver renders
into the managed bridge's DHCP block. If a driver picks a different range,
take a knob here rather than hard-coding two callers.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from testrange.networks.base import Network
from testrange.vms.recipe import VMRecipe

# Mirrors the driver-rendered DHCP pool (currently hardcoded `.100`-`.200`).
_DHCP_RANGE_LO = 100
_DHCP_RANGE_HI = 200


def validate_addressing(networks: Iterable[Network], vms: Iterable[VMRecipe]) -> None:
    """Validate every NIC against the plan's network shape.

    Accumulates all issues and raises one ``ValueError`` containing every
    problem so the user can fix them in one pass.
    """
    nets_by_name = {n.name: n for n in networks}
    vms_list = list(vms)
    problems: list[str] = []

    # Per-network (ipv4 -> origin) so we can cite both sides of a duplicate.
    seen_per_net: dict[str, dict[str, str]] = {}

    for vm in vms_list:
        for idx, nic in enumerate(vm.spec.nics):
            origin = f"VM {vm.name!r} NIC {idx} ({nic.network!r})"
            net = nets_by_name.get(nic.network)
            if net is None:
                problems.append(f"{origin}: references unknown network {nic.network!r}")
                continue

            if nic.ipv4 is None:
                # NIC will use DHCP — only valid if the network actually has
                # DHCP enabled, otherwise it would never get an address at
                # run-phase.
                if not net.dhcp:
                    problems.append(
                        f"{origin}: nic_no_address — network {nic.network!r} "
                        f"has dhcp=False and the NIC declares no static ipv4; "
                        f"this NIC would never get an address. Set ipv4= on "
                        f"the NIC or set dhcp=True on the network."
                    )
                continue

            origin = f"{origin}={nic.ipv4}"
            try:
                subnet = net.network
                addr = ipaddress.IPv4Address(nic.ipv4)
            except ValueError as e:  # pragma: no cover (caught at NIC level)
                problems.append(f"{origin}: {e}")
                continue
            if not isinstance(subnet, ipaddress.IPv4Network):
                problems.append(f"{origin}: network {nic.network!r} is not IPv4")
                continue
            if addr not in subnet:
                problems.append(f"{origin}: address not in subnet {subnet!s}")
                continue
            if addr == subnet.network_address:
                problems.append(f"{origin}: address is the subnet's network address")
                continue
            if addr == subnet.broadcast_address:
                problems.append(f"{origin}: address is the subnet's broadcast address")
                continue
            gw = ipaddress.IPv4Address(net.gateway)
            if addr == gw:
                problems.append(f"{origin}: address collides with gateway {gw!s}")
                continue
            if net.dhcp:
                lo = subnet.network_address + _DHCP_RANGE_LO
                hi = subnet.network_address + _DHCP_RANGE_HI
                if lo <= addr <= hi:
                    problems.append(
                        f"{origin}: address falls inside the DHCP pool "
                        f"({lo!s}-{hi!s}); pick something outside it "
                        f"(typically in {subnet.network_address + 2!s}-"
                        f"{lo - 1!s})"
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
