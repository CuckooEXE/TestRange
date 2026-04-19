"""Abstract base class for virtual network definitions.

Concrete implementations handle the backend-specific work of creating,
configuring, and tearing down an isolated network segment for a test run.
"""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator


class AbstractVirtualNetwork(ABC):
    """Base class for virtual network definitions.

    Subclass this to implement alternative network backends.

    :param name: Human-readable network name.  Used as a DNS domain suffix
        when ``dns=True`` (e.g. ``'NetA'`` makes ``MyVM.NetA`` resolve).
    :param subnet: CIDR notation subnet (e.g. ``'10.0.50.0/24'``).  The
        first usable host address (``.1``) is assigned to the gateway.
    :param dhcp: If ``True``, a DHCP server is enabled on this network.
    :param internet: If ``True``, outbound routing to the host network and
        internet is enabled.  If ``False``, the network is fully isolated.
    :param dns: If ``True``, hostname-based DNS resolution is enabled within
        this network (e.g. ``MyVM.NetA``).
    """

    name: str
    """Human-readable network name; also used as DNS domain suffix when ``dns=True``."""

    subnet: str
    """CIDR subnet string (e.g. ``'10.0.50.0/24'``)."""

    dhcp: bool
    """If ``True``, a DHCP server is enabled on this network."""

    internet: bool
    """If ``True``, outbound routing to the host network and internet is enabled."""

    dns: bool
    """If ``True``, hostname-based DNS resolution is enabled within this network."""

    _network: ipaddress.IPv4Network
    """Parsed :class:`ipaddress.IPv4Network` object derived from :attr:`subnet`."""

    def __init__(
        self,
        name: str,
        subnet: str,
        dhcp: bool = True,
        internet: bool = False,
        dns: bool = True,
    ) -> None:
        self.name = name
        self.subnet = subnet
        self.dhcp = dhcp
        self.internet = internet
        self.dns = dns
        self._network = ipaddress.IPv4Network(subnet, strict=False)

    @property
    def gateway_ip(self) -> str:
        """Return the gateway IP address (first host in the subnet).

        :returns: IP address string (e.g. ``'10.0.50.1'``).
        """
        return str(next(self._network.hosts()))

    @property
    def netmask(self) -> str:
        """Return the subnet mask in dotted-quad notation.

        :returns: Netmask string (e.g. ``'255.255.255.0'``).
        """
        return str(self._network.netmask)

    @property
    def prefix_len(self) -> int:
        """Return the prefix length.

        :returns: Integer prefix length (e.g. ``24``).
        """
        return self._network.prefixlen

    @property
    def dhcp_range_start(self) -> str:
        """Return the first DHCP lease address.

        Starts at the 10th host to leave room for static assignments.

        :returns: IP address string.
        """
        hosts = list(self._network.hosts())
        # Skip gateway (.1) and leave a static block (.2 – .9)
        return str(hosts[9]) if len(hosts) > 9 else str(hosts[1])

    @property
    def dhcp_range_end(self) -> str:
        """Return the last DHCP lease address.

        Uses the penultimate host in the subnet to avoid the broadcast.

        :returns: IP address string.
        """
        hosts = list(self._network.hosts())
        return str(hosts[-1])

    def static_ip_for_index(self, index: int) -> str:
        """Return a deterministic static IP for the *index*-th VM (0-based).

        Static IPs are drawn from ``.2`` onwards (below the DHCP range start).

        :param index: Zero-based VM index within this network.
        :returns: IP address string.
        :raises ValueError: If the index exceeds available static addresses.
        """
        hosts = list(self._network.hosts())
        # index 0 -> hosts[1] (.2), index 1 -> hosts[2] (.3), etc.
        slot = index + 1  # skip gateway
        if slot >= len(hosts):
            raise ValueError(
                f"Static IP index {index} out of range for subnet {self.subnet}"
            )
        return str(hosts[slot])

    @abstractmethod
    def start(self, context: AbstractOrchestrator) -> None:
        """Create and activate the network on the hypervisor.

        :param context: The orchestrator driving this run.  Concrete
            backends downcast to pick up their native handle (a
            ``libvirt.virConnect``, a Proxmox REST client, …).
        :raises NetworkError: If the network cannot be created or activated.
        """

    @abstractmethod
    def stop(self, context: AbstractOrchestrator) -> None:
        """Deactivate and remove the network from the hypervisor.

        Safe to call even if the network is not currently active.

        :param context: The orchestrator driving this run.
        """

    @abstractmethod
    def backend_name(self) -> str:
        """Return the network identifier as registered with the backend.

        This may differ from :attr:`name` if a run-ID suffix has been
        appended to prevent collisions between concurrent test runs.

        :returns: Backend-specific network name string.
        """
