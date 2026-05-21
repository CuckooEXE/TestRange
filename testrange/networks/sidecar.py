"""Pure renderers for the per-Switch sidecar VM.

A Switch with ``dhcp``, ``dns``, or ``nat`` gets one sidecar VM — a
pre-built Alpine image with ``dnsmasq`` (DHCP+DNS) and ``nftables`` (NAT)
baked in. The orchestrator materializes it; this module is the pure,
hypervisor-agnostic part: it turns Plan-time data (the Switch and the VMs
on it) into the four text files the sidecar's config ISO carries:

- ``/etc/dnsmasq.conf`` — DHCP and/or DNS config
- ``/etc/network/interfaces`` — Alpine static NIC stanzas
- ``/etc/nftables.nft`` — NAT MASQUERADE ruleset (empty when ``nat=False``)
- ``/etc/sysctl.d/99-testrange.conf`` — ``ip_forward`` toggle for NAT

…and parses the lease file back.

Nothing here touches libvirt or runs anything. MAC computation is the
driver's job (``compose_mac``); :func:`render_dnsmasq_conf` takes a
``mac_for`` callable the orchestrator injects, so this module never
reaches into the driver stovepipe.

Addressing layout (sidecar ``.1``, mgmt ``.2``, pool ``.10``-``.99``)
comes from :mod:`testrange.networks._addressing_consts` — the same
source :mod:`testrange.networks.validate` reads, so the rendered
``dhcp-range`` and the static-IP validation can never drift apart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from testrange.devices.network import DHCPAddr, StaticAddr
from testrange.networks._addressing_consts import (
    DHCP_RANGE_HI,
    DHCP_RANGE_LO,
)
from testrange.networks.base import Switch
from testrange.vms.recipe import VMRecipe

LEASEFILE = "/var/lib/misc/dnsmasq.leases"

SIDECAR_SWITCH_NIC = "eth0"
SIDECAR_UPLINK_NIC = "eth1"


def sidecar_nic_specs(switch: Switch) -> list[tuple[str, str | None]]:
    """``(network_name, ipv4_or_None)`` for the sidecar's NICs, in attach order.

    First entry is always the switch-facing NIC at ``switch.sidecar_ip``,
    attached to the first Network on the Switch (all Networks on the
    Switch share the bridge, so any will do; first by declaration order).
    When ``switch.nat`` is on, a second entry is appended with ``None``
    for the IPv4 — the orchestrator wires it onto the uplink bridge and
    the sidecar acquires an address via the upstream LAN's DHCP.
    """
    if not switch.networks:
        raise ValueError(f"switch {switch.name!r} has no Networks; cannot place a sidecar NIC")
    specs: list[tuple[str, str | None]] = [(switch.networks[0].name, switch.sidecar_ip)]
    if switch.nat:
        specs.append((_uplink_network_name(switch), None))
    return specs


def _uplink_network_name(switch: Switch) -> str:
    """Hidden network name the orchestrator uses for the sidecar's uplink NIC."""
    return f"__uplink__{switch.name}"


def render_sidecar_interfaces(switch: Switch) -> str:
    """Render the sidecar's Alpine ``/etc/network/interfaces``.

    ``eth0`` is always a static address on the switch (``switch.sidecar_ip``).
    When ``switch.nat`` is on, ``eth1`` is added as ``inet dhcp`` so the
    sidecar can acquire an upstream address for MASQUERADE egress.
    """
    subnet = switch.network
    blocks = [
        "auto lo",
        "iface lo inet loopback",
        "",
        f"auto {SIDECAR_SWITCH_NIC}",
        f"iface {SIDECAR_SWITCH_NIC} inet static",
        f"    address {switch.sidecar_ip}",
        f"    netmask {subnet.netmask}",
        "",
    ]
    if switch.nat:
        blocks.extend(
            [
                f"auto {SIDECAR_UPLINK_NIC}",
                f"iface {SIDECAR_UPLINK_NIC} inet dhcp",
                "",
            ]
        )
    return "\n".join(blocks)


def render_dnsmasq_conf(
    switch: Switch,
    vms: Iterable[VMRecipe],
    mac_for: Callable[[str, int], str],
) -> str:
    """Render the sidecar's ``dnsmasq.conf`` for one Switch.

    ``vms`` is every VM in the plan; NICs on networks that do not belong
    to this Switch are ignored. ``mac_for(vm_name, nic_idx)`` returns the
    stable MAC the driver assigns to that NIC — injected so this module
    stays out of the driver stovepipe.

    Behavior by Switch flags:

    - ``dhcp`` — one ``dhcp-range`` over the Switch's CIDR (Networks share
      the subnet), plus ``dhcp-host`` lines tying each DHCP NIC's MAC to
      its VM name so the lease registers in DNS.
    - ``nat`` — DHCP option 3 (router) advertised as ``.1`` (the sidecar
      is the gateway). When ``nat`` is off, option 3 is suppressed —
      testrange has no router story without NAT.
    - ``dns`` — a ``host-record`` per static NIC
      (``<vmname>.<networkname>`` -> IP). DHCP NICs auto-register via
      ``dhcp-host`` when DHCP is on.
    - ``dhcp`` without ``dns`` — ``port=0`` disables the DNS listener.

    When the Switch has no ``dhcp``, ``dns``, and no DHCP-using NICs, the
    rendered config is the bare minimum to keep dnsmasq quiescent.
    """
    networks_on_switch = {net.name for net in switch.networks}
    lines: list[str] = ["# rendered by testrange — sidecar dnsmasq config"]

    if switch.dhcp:
        lines.append(f"dhcp-leasefile={LEASEFILE}")
        lines.append("dhcp-authoritative")
        lines.append(f"interface={SIDECAR_SWITCH_NIC}")
        lines.append("bind-interfaces")

    if not switch.dns:
        lines.append("port=0")

    if switch.dhcp:
        subnet = switch.network
        lo = subnet.network_address + DHCP_RANGE_LO
        hi = subnet.network_address + DHCP_RANGE_HI
        lines.append(f"dhcp-range={lo!s},{hi!s},{subnet.netmask!s},12h")
        if switch.nat:
            lines.append(f"dhcp-option=option:router,{switch.sidecar_ip}")
        else:
            lines.append("dhcp-option=3")
        if switch.dns:
            lines.append(f"dhcp-option=option:dns-server,{switch.sidecar_ip}")
        else:
            lines.append("dhcp-option=6")

    if switch.dns:
        for net in switch.networks:
            lines.append(f"domain={net.name},{switch.cidr}")

    # VM and network names are interpolated raw below; they are safe because
    # validate_name (testrange._names) rejects `, = # \n` and XML metachars at
    # the LibvirtHypervisor boundary, so they can't break a dnsmasq directive.
    for vm in vms:
        for idx, nic in enumerate(vm.spec.nics):
            if nic.network not in networks_on_switch:
                continue
            if isinstance(nic.addr, StaticAddr):
                if switch.dns:
                    lines.append(f"host-record={vm.name}.{nic.network},{nic.addr.host}")
            elif isinstance(nic.addr, DHCPAddr) and switch.dhcp:
                lines.append(f"dhcp-host={mac_for(vm.name, idx)},{vm.name}")

    return "\n".join(lines) + "\n"


def render_nftables_ruleset(switch: Switch) -> str:
    """Render the sidecar's ``/etc/nftables.nft``.

    When ``switch.nat`` is on, defines one ``nat`` table with a
    ``postrouting`` MASQUERADE on the uplink NIC. Otherwise an empty
    ruleset (``flush ruleset``) — the nftables service still loads it
    cleanly but installs no rules.
    """
    if not switch.nat:
        return "flush ruleset\n"
    return (
        "flush ruleset\n"
        "table ip nat {\n"
        "    chain postrouting {\n"
        "        type nat hook postrouting priority srcnat;\n"
        f'        oifname "{SIDECAR_UPLINK_NIC}" masquerade\n'
        "    }\n"
        "}\n"
    )


def render_sysctl_conf(switch: Switch) -> str:
    """Render the sidecar's ``/etc/sysctl.d/99-testrange.conf``.

    Enables ``net.ipv4.ip_forward`` when ``nat`` is on so the kernel
    forwards between ``eth0`` and ``eth1``. Otherwise an empty file.
    """
    if not switch.nat:
        return ""
    return "net.ipv4.ip_forward=1\n"


def parse_dnsmasq_leases(text: str) -> dict[str, str]:
    """Parse a dnsmasq lease file into ``{mac_lowercase: ipv4}``.

    Lease lines are ``<expiry> <mac> <ip> <hostname> <client-id>``; blank
    or malformed lines are skipped.
    """
    leases: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _expiry, mac, ip = parts[0], parts[1], parts[2]
        if ":" in mac and "." in ip:
            leases[mac.lower()] = ip
    return leases


__all__ = [
    "LEASEFILE",
    "SIDECAR_SWITCH_NIC",
    "SIDECAR_UPLINK_NIC",
    "parse_dnsmasq_leases",
    "render_dnsmasq_conf",
    "render_nftables_ruleset",
    "render_sidecar_interfaces",
    "render_sysctl_conf",
    "sidecar_nic_specs",
]
