"""Abstract base classes for virtual network definitions.

Two layers, mirroring the standard L2-virtualisation model:

* :class:`AbstractSwitch` — an L2 switch (or its backend equivalent).
  Carries optional uplink physical NIC bindings and a backend-specific
  *type* knob for backends that ship multiple switch flavours
  (e.g. PVE SDN's ``simple`` / ``vlan`` / ``vxlan`` / ``evpn`` zones).
  Networks attach to a switch; one switch can host many networks.
  Concrete impls live in each backend's module.

* :class:`AbstractVirtualNetwork` — a network VMs attach to.  In
  ESXi-shaped backends this is the **port group**; in libvirt and
  pre-SDN Proxmox setups it's just "the bridge".  Either way it's
  the named thing :class:`~testrange.devices.vNIC` references.

Networks may declare a :attr:`switch` they live on.  Backends that
don't model switches (libvirt's vanilla bridges) ignore the field;
backends that do (Proxmox SDN, future VMware) use it to place the
network in the right zone / vSwitch.

Concrete implementations handle the backend-specific work of creating,
configuring, and tearing down both layers for a test run.
"""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from testrange.orchestrator_base import AbstractOrchestrator


class AbstractSwitch(ABC):
    """Base class for L2 switches (or their backend equivalent).

    A switch hosts one or more :class:`AbstractVirtualNetwork`
    instances.  Backends that don't model switches as a separate
    layer (libvirt's vanilla bridges, where every "network" *is* a
    bridge) can leave this unused — :class:`AbstractVirtualNetwork`
    accepts ``switch=None`` and lets each backend pick its own
    default behaviour.

    Backends that *do* model switches (Proxmox SDN zones, future
    VMware vSwitches) subclass this and implement :meth:`start` /
    :meth:`stop` to create + destroy the underlying construct.

    :param name: Human-readable switch name.  Used as the basis for
        the backend's switch identifier (subject to length / charset
        constraints — see :meth:`backend_name`).
    :param switch_type: Optional backend-specific switch flavour.
        Examples: ``"simple"`` / ``"vlan"`` / ``"vxlan"`` / ``"evpn"``
        for Proxmox SDN zones; ``"standard"`` / ``"distributed"`` for
        a hypothetical VMware backend.  ``None`` means "backend's
        default" — that's what existing simple-zone tests get without
        opting into anything.
    :param uplinks: Optional list of physical NIC names on the
        hypervisor host that this switch should bind as uplinks.
        Backends that route external traffic through a specific
        physical interface (Proxmox VLAN/VXLAN zones, VMware
        vSwitches) honour this; backends that route through the
        host's default route (libvirt NAT, Proxmox simple zones)
        ignore it.
    """

    name: str
    """Human-readable switch name."""

    switch_type: str | None
    """Optional backend-specific switch flavour selector."""

    uplinks: list[str]
    """Optional physical-NIC uplinks (empty list when none declared)."""

    def __init__(
        self,
        name: str,
        switch_type: str | None = None,
        uplinks: Sequence[str] | None = None,
    ) -> None:
        self.name = name
        self.switch_type = switch_type
        self.uplinks = list(uplinks) if uplinks else []

    @abstractmethod
    def start(self, context: AbstractOrchestrator) -> None:
        """Create the switch on the hypervisor.

        Idempotent: if a switch with the same identifier already
        exists, accept it as ours rather than failing.

        :param context: The orchestrator driving this run.
        :raises NetworkError: If the switch cannot be created.
        """

    @abstractmethod
    def stop(self, context: AbstractOrchestrator) -> None:
        """Remove the switch from the hypervisor.

        Best-effort.  Implementations should swallow per-resource
        errors so teardown never raises.

        :param context: The orchestrator driving this run.
        """

    @abstractmethod
    def backend_name(self) -> str:
        """Return the switch identifier as registered with the backend.

        May differ from :attr:`name` if backend-specific length /
        charset constraints required mangling (e.g. PVE SDN zone IDs
        cap at 8 characters of lowercase ASCII alphanumerics).
        """


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
    :param switch: Optional :class:`AbstractSwitch` (or its name) this
        network lives on.  Backends that model switches as a distinct
        layer (Proxmox SDN, future VMware) honour the binding;
        backends without a separate switch concept (libvirt) treat
        the field as decoration.  ``None`` means "backend's default
        switch" — backwards-compatible with every existing
        ``VirtualNetwork(name, subnet, ...)`` call.
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

    switch: AbstractSwitch | str | None
    """Switch this network lives on; see constructor docstring."""

    _network: ipaddress.IPv4Network
    """Parsed :class:`ipaddress.IPv4Network` object derived from :attr:`subnet`."""

    def __init__(
        self,
        name: str,
        subnet: str,
        dhcp: bool = True,
        internet: bool = False,
        dns: bool = True,
        switch: AbstractSwitch | str | None = None,
    ) -> None:
        self.name = name
        self.subnet = subnet
        self.dhcp = dhcp
        self.internet = internet
        self.dns = dns
        self.switch = switch
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
            backends downcast to pick up their own control-plane
            handle.
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
