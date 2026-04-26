"""Reference attaching a VM to a named virtual network."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class AbstractVirtualNetworkRef(AbstractDevice):
    """Sealed base class for NIC-shaped device specs.

    Backend-specific NIC subclasses (with options like model
    selection, queue depth, VLAN tagging, firewall toggles) live in
    their backend module and extend this directly — siblings of
    :class:`VirtualNetworkRef`, not children.
    """

    name: str
    """Name of the network this NIC attaches to."""

    ip: str | None
    """Optional static IPv4 address for this NIC; ``None`` means DHCP."""

    @property
    def device_type(self) -> str:
        return "network_ref"


class VirtualNetworkRef(AbstractVirtualNetworkRef):
    """Generic NIC spec — accepted by every backend.

    Attaches a VM to a named
    :class:`~testrange.networks.base.AbstractVirtualNetwork`.  A VM
    can have multiple ``VirtualNetworkRef`` entries; each results in
    one virtual NIC.

    :param name: The ``name`` of the network to attach to.  Must
        match a network declared in the orchestrator's ``networks=``
        list.
    :param ip: Optional static IPv4 address (e.g. ``"10.0.100.55"``).
        ``None`` (default) means DHCP / deterministic reservation.

    Example::

        VirtualNetworkRef("OfflineNet", ip="10.0.100.55")
        VirtualNetworkRef("NetA")   # DHCP
    """

    def __init__(self, name: str, ip: str | None = None) -> None:
        self.name = name
        self.ip = ip

    def __repr__(self) -> str:
        if self.ip:
            return f"VirtualNetworkRef({self.name!r}, ip={self.ip!r})"
        return f"VirtualNetworkRef({self.name!r})"


__all__ = ["AbstractVirtualNetworkRef", "VirtualNetworkRef"]
