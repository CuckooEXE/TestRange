"""Reference attaching a VM to a named virtual network."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class VirtualNetworkRef(AbstractDevice):
    """Attaches a VM to a named
    :class:`~testrange.networks.base.AbstractVirtualNetwork`.

    A VM can have multiple ``VirtualNetworkRef`` entries in its ``devices``
    list.  Each entry results in one virtual NIC on the VM.

    :param name: The ``name`` of the network to attach to.  Must match
        a network declared in the orchestrator's ``networks=`` list.
    :param ip: An optional static IPv4 address to assign to this NIC (e.g.
        ``'10.0.100.55'``).  If ``None`` (the default), the address is
        obtained via DHCP or a deterministic reservation.

    Example::

        VirtualNetworkRef("OfflineNet", ip="10.0.100.55")
        VirtualNetworkRef("NetA")   # DHCP
    """

    name: str
    """Name of the network this NIC attaches to."""

    ip: str | None
    """Optional static IPv4 address for this NIC; ``None`` means DHCP."""

    def __init__(self, name: str, ip: str | None = None) -> None:
        self.name = name
        self.ip = ip

    @property
    def device_type(self) -> str:
        """Return ``'network_ref'``.

        :returns: The string ``'network_ref'``.
        """
        return "network_ref"

    def __repr__(self) -> str:
        if self.ip:
            return f"VirtualNetworkRef({self.name!r}, ip={self.ip!r})"
        return f"VirtualNetworkRef({self.name!r})"


__all__ = ["VirtualNetworkRef"]
