"""Network-level plan validation, plus the whole-plan entry point.

Single-NIC checks (parseable IPv4) live in ``NetworkIface.__post_init__``.
The network checks that need the full plan in hand — subnet membership
against the owning Switch, reserved-slot collisions (sidecar/mgmt),
DHCP-pool collision, duplicate static IPs, network-name uniqueness/safety,
and that every NIC references a declared network — live here, so a user
sees every problem in one pass instead of fix-one-retry-find-next.

:func:`validate_hypervisor_plan` is the entry point a Hypervisor runs at
construction; it delegates the VM- and pool-level (non-network) checks to
:func:`testrange.vms.validate.validate_vm_plan`.

The DHCP pool bounds and reserved offsets come from
:mod:`testrange.networks._addressing_consts` so the validator and the
sidecar's dnsmasq config can never drift apart.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

from testrange.devices.network import StaticAddr
from testrange.devices.pool.base import StoragePool
from testrange.networks._addressing_consts import (
    DHCP_RANGE_HI,
    DHCP_RANGE_LO,
    MGMT_OFFSET,
    SIDECAR_OFFSET,
    USER_STATIC_HI,
    USER_STATIC_LO,
)
from testrange.networks.base import Switch
from testrange.vms.recipe import VMRecipe

# Names flow verbatim into the sidecar's dnsmasq.conf (host-record/dhcp-host/
# domain lines) where `, = # \n` would break or inject a directive, and into
# each backend's resource layer. The shared rule allows a DNS-label-safe set
# (`[A-Za-z0-9_.-]`, starting with a letter/digit/underscore); a backend with
# stricter limits (Proxmox vnet length, libvirt XML) layers its own check on
# top at its own boundary. A leading `_` is allowed for value objects, but the
# `__` prefix is reserved against *user* names (orchestrator internals use it).
_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def validate_name(value: str, kind: str) -> str:
    """Return ``value`` unchanged, or raise ``ValueError`` if unsafe.

    ``kind`` names the field for the error message (e.g. ``"Network.name"``).
    """
    if not value:
        raise ValueError(f"{kind} must be a non-empty string")
    if not _SAFE_NAME.fullmatch(value):
        raise ValueError(
            f"{kind} {value!r} has illegal characters: allowed are letters, digits, "
            "'_', '.', '-', starting with a letter, digit, or underscore. These names "
            "are interpolated into dnsmasq.conf and backend resource identifiers."
        )
    return value


def validate_hypervisor_plan(
    networks: Iterable[Switch],
    pools: Iterable[StoragePool],
    vms: Iterable[VMRecipe],
) -> None:
    """Backend-agnostic plan validation a Hypervisor runs at construction.

    Network checks live here; the VM- and pool-level (non-network) checks are
    delegated to :func:`testrange.vms.validate.validate_vm_plan`. Together they
    cover structural coherence (every VM NIC/OSDrive references a declared
    network/pool), name uniqueness/safety, the reserved ``__`` prefix and
    ``-data<N>`` marker, and per-NIC addressing (:func:`validate_addressing`).
    Raises ``ValueError`` on the first structural problem; addressing problems
    are accumulated together.
    """
    # Local import breaks the cycle: vms.validate imports validate_name from
    # this module, so this module must not import vms.validate at load time.
    from testrange.vms.validate import validate_vm_plan

    switches = tuple(networks)
    rs = tuple(vms)

    net_names = {n.name for s in switches for n in s.networks}
    all_nets = [n.name for s in switches for n in s.networks]

    validate_vm_plan(rs, pools)

    dup_nets = {n for n in all_nets if all_nets.count(n) > 1}
    if dup_nets:
        raise ValueError(f"hypervisor networks have duplicate names: {sorted(dup_nets)}")

    for s in switches:
        validate_name(s.name, "Switch.name")
    for n in all_nets:
        validate_name(n, "Network.name")

    # The orchestrator synthesizes internal switches/networks under a `__`
    # prefix (__build, __uplink__<sw>); reserve it. (VM names are checked by
    # validate_vm_plan.)
    reserved = sorted({n for n in (*(s.name for s in switches), *all_nets) if n.startswith("__")})
    if reserved:
        raise ValueError(
            f"names starting with '__' are reserved for testrange internals; rename: {reserved}"
        )

    for r in rs:
        for nic in r.spec.nics:
            if nic.network not in net_names:
                raise ValueError(
                    f"VM {r.name!r} references unknown network {nic.network!r}; "
                    f"declared networks: {sorted(net_names)}"
                )

    validate_addressing(switches, rs)


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
    # Keyed by Switch name, not Network name: all Networks on one Switch share a
    # single CIDR/L2 wire (base.py), so a static IP reused across two Networks of
    # the *same* Switch still collides on the wire (H8). Switch names are unique
    # per plan.
    seen_per_switch: dict[str, dict[str, str]] = {}

    for vm in vms_list:
        for idx, nic in enumerate(vm.spec.nics):
            origin = f"VM {vm.name!r} NIC {idx} ({nic.network!r})"
            switch_opt = switch_for.get(nic.network)
            if switch_opt is None:
                problems.append(f"{origin}: references unknown network {nic.network!r}")
                continue
            switch = switch_opt

            if not isinstance(nic.addr, StaticAddr):
                # DHCPAddr or None: nothing to validate at the plan level. A
                # DHCP NIC gets a lease; an unconfigured (None) NIC has no
                # address. Neither can collide with a reserved slot, and the
                # guest OS's behavior is not the plan validator's to police.
                continue

            static_ip = nic.addr.host
            origin = f"{origin}={static_ip}"
            subnet = switch.network
            try:
                addr = ipaddress.IPv4Address(static_ip)
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
                    problems.append(f"{origin}: address collides with sidecar slot {sidecar!s}")
                    continue
            if switch.mgmt:
                mgmt = subnet.network_address + MGMT_OFFSET
                if addr == mgmt:
                    problems.append(f"{origin}: address collides with mgmt slot {mgmt!s}")
                    continue
            if switch.sidecar is not None and switch.sidecar.dhcp:
                lo = subnet.network_address + DHCP_RANGE_LO
                hi = subnet.network_address + DHCP_RANGE_HI
                if lo <= addr <= hi:
                    problems.append(
                        f"{origin}: address falls inside the DHCP pool "
                        f"({lo!s}-{hi!s}); pick something in "
                        f"{subnet.network_address + USER_STATIC_LO!s}-"
                        f"{subnet.network_address + USER_STATIC_HI!s}"
                    )
                    continue

            seen = seen_per_switch.setdefault(switch.name, {})
            prior = seen.get(static_ip)
            if prior is not None:
                problems.append(f"{origin}: duplicate — address already used by {prior}")
                continue
            seen[static_ip] = origin

    if problems:
        joined = "\n  - ".join(problems)
        raise ValueError(f"plan has {len(problems)} addressing problem(s):\n  - {joined}")


__all__ = ["validate_addressing", "validate_hypervisor_plan", "validate_name"]
