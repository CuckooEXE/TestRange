"""Pure renderers for the per-Switch sidecar VM.

A Switch carrying a ``Sidecar`` (dhcp/dns/nat services) gets one sidecar
VM — a pre-built Alpine image with ``dnsmasq`` (DHCP+DNS) and ``nftables``
(NAT) baked in. The orchestrator materializes it; this module is the pure,
hypervisor-agnostic part: it turns Plan-time data (the Switch and the VMs
on it) into the four text files the sidecar's config ISO carries:

- ``/etc/dnsmasq.conf`` — DHCP and/or DNS config
- ``/etc/network/interfaces`` — Alpine static NIC stanzas
- ``/etc/nftables.nft`` — NAT MASQUERADE ruleset (empty when ``nat=False``)
- ``/etc/sysctl.d/99-testrange.conf`` — ``ip_forward`` toggle for NAT

…and parses the lease file back.

Nothing here touches the hypervisor or runs anything. MAC computation is the
driver's job (``compose_mac``); :func:`render_dnsmasq_conf` takes a
``mac_for`` callable the orchestrator injects, so this module never
reaches into the driver stovepipe.

The reserved-slot and DHCP-pool layout (``SIDECAR_OFFSET``, ``MGMT_OFFSET``,
``DHCP_RANGE_LO``..``DHCP_RANGE_HI``) comes from
:mod:`testrange.networks._addressing_consts` — the same source
:mod:`testrange.networks.validate` reads, so the rendered ``dhcp-range`` and
the static-IP validation can never drift apart.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable

from testrange.devices.network import DHCPAddr, StaticAddr
from testrange.networks._addressing_consts import (
    DHCP_RANGE_HI,
    DHCP_RANGE_LO,
)
from testrange.networks.base import Sidecar, Switch
from testrange.vms.recipe import VMRecipe

LEASEFILE = "/var/lib/misc/dnsmasq.leases"
# Where the sidecar applies the dnsmasq config delivered via its config ISO.
# Reading it back over the native guest agent is the run-phase readiness probe
# (ADR-0010 §8): a non-empty read proves the agent answers AND the sidecar has
# applied its config — so DHCP is being served before any user VM boots.
SIDECAR_DNSMASQ_CONF = "/etc/dnsmasq.conf"

SIDECAR_SWITCH_NIC = "eth0"
SIDECAR_UPLINK_NIC = "eth1"


def _sidecar(switch: Switch) -> Sidecar:
    """The Switch's :class:`Sidecar`, asserting the caller honored the contract.

    Every renderer in this module reads the sidecar's services and is invoked
    only when ``switch.needs_sidecar`` holds — provision.py guards on it before
    rendering, and the transient build switch always carries one. So the sidecar
    is non-None here; the assert turns a contract violation into a loud failure
    instead of an ``AttributeError`` deep in a renderer.
    """
    sidecar = switch.sidecar
    assert sidecar is not None, (
        f"sidecar renderer called on switch {switch.name!r} with no Sidecar — "
        "callers must guard on switch.needs_sidecar"
    )
    return sidecar


def sidecar_nic_specs(switch: Switch) -> list[tuple[str, str | None]]:
    """``(network_name, ipv4_or_None)`` for the sidecar's NICs, in attach order.

    First entry is always the switch-facing NIC at ``switch.sidecar_ip``,
    attached to the first Network on the Switch (all Networks on the
    Switch share the bridge, so any will do; first by declaration order).
    When the sidecar has ``nat``, a second entry is appended for the uplink
    NIC, wired onto the uplink bridge: ``None`` for the IPv4 (the sidecar DHCPs
    from the upstream LAN), or ``sidecar.addr.host`` when a static uplink
    address is set (NET-7 — for host-NAT'd uplinks that won't DHCP the sidecar).
    """
    sidecar = _sidecar(switch)
    if not switch.networks:
        raise ValueError(f"switch {switch.name!r} has no Networks; cannot place a sidecar NIC")
    specs: list[tuple[str, str | None]] = [(switch.networks[0].name, switch.sidecar_ip)]
    if sidecar.nat:
        uplink_ip = sidecar.addr.host if sidecar.addr is not None else None
        specs.append((_uplink_network_name(switch), uplink_ip))
    return specs


def _uplink_network_name(switch: Switch) -> str:
    """Hidden network name the orchestrator uses for the sidecar's uplink NIC.

    Shared with ``orchestrator.provision`` (which registers the uplink network
    backend under this key) and ``orchestrator.build_phase`` (which pops it at
    teardown), so the ``__uplink__<switch>`` convention lives in one place.
    """
    return f"__uplink__{switch.name}"


def render_sidecar_interfaces(switch: Switch) -> str:
    """Render the sidecar's Alpine ``/etc/network/interfaces``.

    ``eth0`` is always a static address on the switch (``switch.sidecar_ip``).
    When the sidecar has ``nat``, ``eth1`` (the MASQUERADE uplink) is ``inet
    dhcp`` by default — the sidecar acquires an upstream address from the LAN.
    When ``sidecar.addr`` is set (NET-7), ``eth1`` is a static stanza instead,
    for hosts that won't DHCP the sidecar's MAC (the uplink bridge is host-NAT'd).
    """
    sidecar = _sidecar(switch)
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
    if sidecar.nat:
        if sidecar.addr is not None:
            iface = ipaddress.IPv4Interface(sidecar.addr.cidr(None))
            blocks.extend(
                [
                    f"auto {SIDECAR_UPLINK_NIC}",
                    f"iface {SIDECAR_UPLINK_NIC} inet static",
                    f"    address {iface.ip}",
                    f"    netmask {iface.network.netmask}",
                ]
            )
            if sidecar.addr.gw is not None:
                blocks.append(f"    gateway {sidecar.addr.gw}")
            blocks.append("")
        else:
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
    sidecar = _sidecar(switch)
    networks_on_switch = {net.name for net in switch.networks}
    lines: list[str] = ["# rendered by testrange — sidecar dnsmasq config"]

    if sidecar.dhcp:
        lines.append(f"dhcp-leasefile={LEASEFILE}")
        lines.append("dhcp-authoritative")
        lines.append(f"interface={SIDECAR_SWITCH_NIC}")
        lines.append("bind-interfaces")

    if not sidecar.dns:
        lines.append("port=0")

    # With a static uplink (NET-7) the sidecar's eth1 doesn't DHCP, so nothing
    # populates /etc/resolv.conf and dnsmasq has no upstream to forward to.
    # Point it explicitly at the uplink's DNS server(s) and ignore resolv.conf.
    if sidecar.dns and sidecar.addr is not None and sidecar.addr.dns:
        lines.append("no-resolv")
        lines.extend(f"server={s}" for s in sidecar.addr.dns)

    if sidecar.dhcp:
        subnet = switch.network
        lo = subnet.network_address + DHCP_RANGE_LO
        hi = subnet.network_address + DHCP_RANGE_HI
        lines.append(f"dhcp-range={lo!s},{hi!s},{subnet.netmask!s},12h")
        if sidecar.nat:
            lines.append(f"dhcp-option=option:router,{switch.sidecar_ip}")
        else:
            lines.append("dhcp-option=3")
        if sidecar.dns:
            lines.append(f"dhcp-option=option:dns-server,{switch.sidecar_ip}")
        else:
            lines.append("dhcp-option=6")

    if sidecar.dns and switch.networks:
        # Every Network on a Switch shares one subnet (switch.cidr), but dnsmasq
        # assigns exactly one DNS domain per address-range. Emitting one `domain=`
        # per Network produced conflicting directives for the *same* range: dnsmasq
        # prepends each and first-matches, so only the last-declared label ever
        # took effect, and some strict builds reject the duplicate outright. Emit a
        # single canonical domain for the subnet — the last Network's label, i.e.
        # the one already winning — so the config is honest and deterministic and
        # the live DHCP-auto-FQDN behavior is unchanged (NET-19).
        lines.append(f"domain={switch.networks[-1].name},{switch.cidr}")

    # VM and network names are interpolated raw below. Each driver enforces
    # its own name-charset rules at its boundary (dnsmasq-directive metachars
    # like `, = # \n` must be rejected there) so they can't break a directive.
    for vm in vms:
        for idx, nic in enumerate(vm.spec.nics):
            if nic.network not in networks_on_switch:
                continue
            if isinstance(nic.addr, StaticAddr):
                if sidecar.dns:
                    lines.append(f"host-record={vm.name}.{nic.network},{nic.addr.host}")
            elif isinstance(nic.addr, DHCPAddr) and sidecar.dhcp:
                lines.append(f"dhcp-host={mac_for(vm.name, idx)},{vm.name}")

    return "\n".join(lines) + "\n"


def render_nftables_ruleset(switch: Switch) -> str:
    """Render the sidecar's ``/etc/nftables.nft``.

    When the sidecar has ``nat``, defines one ``nat`` table with a
    ``postrouting`` MASQUERADE on the uplink NIC. Otherwise an empty
    ruleset (``flush ruleset``) — the nftables service still loads it
    cleanly but installs no rules.
    """
    if not _sidecar(switch).nat:
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

    Enables ``net.ipv4.ip_forward`` when the sidecar has ``nat`` so the kernel
    forwards between ``eth0`` and ``eth1``. Otherwise an empty file.
    """
    if not _sidecar(switch).nat:
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
    "SIDECAR_DNSMASQ_CONF",
    "SIDECAR_SWITCH_NIC",
    "SIDECAR_UPLINK_NIC",
    "parse_dnsmasq_leases",
    "render_dnsmasq_conf",
    "render_nftables_ruleset",
    "render_sidecar_interfaces",
    "render_sysctl_conf",
    "sidecar_nic_specs",
]
