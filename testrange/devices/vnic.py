"""Virtual NIC device — attaches a VM to a named virtual network."""

from __future__ import annotations

from testrange.devices.base import AbstractDevice


class AbstractVNIC(AbstractDevice):
    """Sealed base class for NIC-shaped device specs.

    Backend-specific NIC subclasses (with options like model
    selection, queue depth, VLAN tagging, firewall toggles) live in
    their backend module and extend this directly — siblings of
    :class:`vNIC`, not children.
    """

    ref: str
    """Name of the network this NIC attaches to (matched against the
    orchestrator's ``networks=`` list by name)."""

    ip: str | None
    """Optional static IPv4 address for this NIC; ``None`` means DHCP."""

    @property
    def device_type(self) -> str:
        return "vnic"


class vNIC(AbstractVNIC):
    """Generic virtual NIC — accepted by every backend.

    Attaches a VM to a named
    :class:`~testrange.networks.base.AbstractVirtualNetwork`.  A VM
    can have multiple ``vNIC`` entries; each results in one virtual
    NIC on the guest.

    :param ref: The ``name`` of the network to attach to.  Must match
        a network declared in the orchestrator's ``networks=`` list.
    :param ip: Optional static IPv4 address (e.g. ``"10.0.100.55"``).
        ``None`` (default) means DHCP / deterministic reservation.

    Example::

        vNIC("OfflineNet", ip="10.0.100.55")
        vNIC("NetA")   # DHCP
    """

    def __init__(self, ref: str, ip: str | None = None) -> None:
        self.ref = ref
        self.ip = ip

    def __repr__(self) -> str:
        if self.ip:
            return f"vNIC({self.ref!r}, ip={self.ip!r})"
        return f"vNIC({self.ref!r})"


__all__ = ["AbstractVNIC", "vNIC"]
