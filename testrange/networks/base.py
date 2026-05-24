"""Network and Switch — Plan-level network declarations on a Hypervisor.

A Switch is an L2 broadcast domain that owns the networking-infrastructure
decisions (``cidr``, ``uplink``, ``mgmt``, ``dhcp``, ``dns``, ``nat``).
A Network is a logical label — a port-group — within a Switch. Every
Network on a Switch shares the Switch's CIDR (one wire, multiple labels;
ESXi port-groups on one VLAN). VMs attach to a Network by name; the
orchestrator resolves which Switch owns it.

The bare Switch is a pure L2 broadcast domain with nothing attached.
Setting any of ``dhcp``/``dns``/``nat`` causes a sidecar VM to be
materialized at ``.1`` of the Switch's subnet. ``mgmt=True`` puts a
host adapter at ``.2``. ``uplink="<nic>"`` asks the driver to bridge
the Switch to a physical NIC.

``uplink`` is a physical NIC name on the hypervisor host; the driver owns
what it does with it (host bridge + NIC enslavement for libvirt, a ``vmnic``
on a vSwitch for ESXi, a vmbr port for Proxmox, an External VMSwitch for
Hyper-V). The orchestrator never realizes L2 itself.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from testrange.devices.network import StaticAddr
from testrange.networks._addressing_consts import (
    MGMT_OFFSET,
    SIDECAR_OFFSET,
)

_DEFAULT_CIDR = "192.168.10.0/24"


@dataclass(frozen=True)
class Network:
    """A logical label (port-group) on a Switch.

    Networks within a Switch share the Switch's CIDR. The name is the
    handle VMs use to attach (``NetworkIface(network="netA", ...)``).
    """

    name: str

    def __post_init__(self) -> None:
        # Only the backend-agnostic check here: a name must be non-empty.
        # Backend charset rules (dnsmasq/XML/vnet-length safety) are the
        # driver's concern and live at each driver's boundary.
        if not self.name:
            raise ValueError("Network.name must be a non-empty string")


@dataclass(frozen=True)
class NetworkAddressing:
    """Per-network addressing facts a builder needs to render a guest netplan.

    Hypervisor-agnostic; derived from a :class:`Network` plus its owning
    :class:`Switch`. The orchestrator brokers — it builds a
    ``Mapping[network_name, NetworkAddressing]`` and hands it to the
    builder so the builder never has to know about hypervisor types.

    Fields reflect the Switch's infrastructure:

    - ``dhcp`` — a NIC with no static ``ipv4`` gets a lease only when the
      owning Switch has ``dhcp``; otherwise the NIC has no address.
    - ``gateway`` — the sidecar ``.1`` when the Switch has ``nat``;
      ``None`` otherwise (no router story).
    - ``dns_server`` — the sidecar ``.1`` when the Switch has ``dns``;
      ``None`` otherwise.
    """

    cidr: str
    prefix_len: int
    dhcp: bool
    gateway: str | None
    dns_server: str | None

    @classmethod
    def from_switch(cls, switch: Switch) -> NetworkAddressing:
        return cls(
            cidr=switch.cidr,
            prefix_len=switch.network.prefixlen,
            dhcp=switch.dhcp,
            gateway=switch.sidecar_ip if switch.nat else None,
            dns_server=switch.sidecar_ip if switch.dns else None,
        )


@dataclass(frozen=True)
class Switch:
    """An L2 broadcast domain that owns the networking-infrastructure knobs.

    Holds one or more Networks (logical labels) sharing a single CIDR.
    All infrastructure flags default off — the bare Switch is pure L2
    with nothing attached:

    - ``cidr`` — the IPv4 subnet for every Network on this Switch.
      Strict network form (``192.168.10.0/24``); host-form raises.
    - ``uplink`` — physical NIC on the hypervisor host. When set, the
      driver attaches the Switch to that NIC. Without ``nat``, guests
      egress with their own MACs and IPs (pure L2 to the LAN). With
      ``nat``, the guest segment stays isolated and the sidecar
      MASQUERADEs out a second NIC on a driver-provided uplink segment.
    - ``mgmt`` — host adapter at ``.2`` on the Switch's subnet. Just an
      adapter — no NAT, no forwarding, no router semantics.
    - ``dns`` — sidecar serves DNS at ``.1`` (one ``<vmname>.<networkname>``
      record per VM).
    - ``dhcp`` — sidecar serves DHCP at ``.1``; pool is ``.10``-``.99``.
    - ``nat`` — sidecar MASQUERADEs guest traffic out the uplink at
      ``.1`` (the sidecar is the gateway). **Requires** ``uplink``.
    """

    name: str
    networks: tuple[Network, ...] = field(default_factory=tuple)
    cidr: str = _DEFAULT_CIDR
    uplink: str | None = None
    mgmt: bool = False
    dns: bool = False
    dhcp: bool = False
    nat: bool = False
    uplink_addr: StaticAddr | None = None

    def __init__(
        self,
        name: str,
        *networks: Network,
        cidr: str = _DEFAULT_CIDR,
        uplink: str | None = None,
        mgmt: bool = False,
        dns: bool = False,
        dhcp: bool = False,
        nat: bool = False,
        uplink_addr: StaticAddr | None = None,
    ) -> None:
        if not name:
            raise ValueError("Switch.name must be a non-empty string")
        if uplink is not None and not uplink:
            raise ValueError("Switch.uplink must be a non-empty string or None")

        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
        except ValueError as e:
            raise ValueError(
                f"Switch.cidr must be a valid IPv4 network in strict form "
                f"(network address, not a host address): got {cidr!r}: {e}"
            ) from e
        if not isinstance(parsed, ipaddress.IPv4Network):
            raise ValueError(f"Switch.cidr must be IPv4 (v0 limitation); got {cidr!r}")

        if nat and uplink is None:
            raise ValueError(
                f"Switch({name!r}, nat=True) requires uplink=<nic-name> — the "
                "sidecar needs a physical NIC to MASQUERADE traffic out of."
            )
        if uplink_addr is not None:
            # The sidecar's uplink NIC (eth1) gets a static address instead of
            # DHCP from the upstream LAN (NET-7) — for hosts that won't lease the
            # sidecar's MAC (single-public-IP / MAC-filtered boxes, where the
            # uplink bridge is host-NAT'd). It only exists on the NAT egress NIC,
            # and it sits on the uplink's subnet, not the Switch CIDR — so it must
            # carry its own prefix.
            if not nat:
                raise ValueError(
                    f"Switch({name!r}, uplink_addr=...) requires nat=True — the "
                    "static address configures the sidecar's MASQUERADE uplink NIC."
                )
            if "/" not in uplink_addr.addr:
                raise ValueError(
                    f"Switch({name!r}).uplink_addr needs an explicit prefix "
                    f"(e.g. StaticAddr('10.10.10.2/24', gw=...)); got {uplink_addr.addr!r} "
                    "— the uplink is its own subnet, not the Switch CIDR, so the "
                    "netmask cannot be derived."
                )

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "networks", tuple(networks))
        object.__setattr__(self, "cidr", cidr)
        object.__setattr__(self, "uplink", uplink)
        object.__setattr__(self, "mgmt", bool(mgmt))
        object.__setattr__(self, "dns", bool(dns))
        object.__setattr__(self, "dhcp", bool(dhcp))
        object.__setattr__(self, "nat", bool(nat))
        object.__setattr__(self, "uplink_addr", uplink_addr)

    @property
    def network(self) -> ipaddress.IPv4Network:
        net = ipaddress.ip_network(self.cidr, strict=True)
        assert isinstance(net, ipaddress.IPv4Network)
        return net

    @property
    def sidecar_ip(self) -> str:
        """The sidecar's pinned address: ``network_address + 1``."""
        return str(self.network.network_address + SIDECAR_OFFSET)

    @property
    def mgmt_ip(self) -> str:
        """The host mgmt adapter's pinned address: ``network_address + 2``."""
        return str(self.network.network_address + MGMT_OFFSET)

    @property
    def needs_sidecar(self) -> bool:
        """Whether this Switch requires a sidecar VM (DHCP, DNS, or NAT)."""
        return self.dhcp or self.dns or self.nat
