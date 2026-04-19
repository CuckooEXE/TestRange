"""Proxmox VE virtual network (SCAFFOLDING).

.. warning::

   Not yet implemented.  :meth:`start` and :meth:`stop` raise
   :class:`NotImplementedError`.

Design notes
------------

Proxmox's answer to libvirt's dnsmasq-backed networks is the
**Software-Defined Networking** subsystem (`pve-sdn`).  Concretely:

- An ``AbstractVirtualNetwork`` maps to an *SDN vnet* inside a *zone*
  (Simple zone is the closest equivalent to a libvirt NAT network;
  VLAN / VXLAN zones exist for more advanced setups).
- DHCP reservations are made through the SDN IPAM (``pve`` IPAM by
  default) — ``POST /cluster/sdn/ipams/{ipam}/subnets/.../ips``.
- Traffic policy (``internet=True`` / ``internet=False``) becomes
  zone + NAT configuration or explicit firewall rules.
- DNS (``dns=True``) requires either a cluster-wide DNS zone or
  falling back to per-VM ``guest`` fields surfaced via ``qm set``.

All mutations require a ``POST /cluster/sdn`` reload to become
effective — this is the Proxmox equivalent of libvirt's
``network.create()`` and the reason SDN vnet creation is a two-step
dance.

TODO list for implementation
----------------------------

1. On :meth:`start`, lazy-import ``proxmoxer`` (via the shared
   orchestrator client), ``POST /cluster/sdn/vnets`` + ``POST
   /cluster/sdn/subnets`` with the VM's subnet CIDR, then reload SDN.
2. Walk ``self._vm_entries`` and create IPAM reservations for each
   static IP.
3. On :meth:`stop`, delete the vnet + subnet + any leftover IPAM
   entries; reload SDN again.
4. :meth:`backend_name` returns the SDN vnet name (Proxmox caps SDN
   names at 8 characters — tighter than libvirt's 15 — so the
   run-suffix scheme needs adjusting).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.networks.base import AbstractVirtualNetwork

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator


class ProxmoxVirtualNetwork(AbstractVirtualNetwork):
    """Proxmox-VE SDN-backed virtual network (SCAFFOLDING).

    Constructor mirrors
    :class:`~testrange.backends.libvirt.VirtualNetwork`; lifecycle
    methods raise :class:`NotImplementedError`.
    """

    def start(self, context: AbstractOrchestrator) -> None:
        # TODO: POST /cluster/sdn/vnets with the SDN vnet definition
        # (zone=<our-simple-zone>, alias=<self.name>).
        # TODO: POST /cluster/sdn/subnets for self.subnet.
        # TODO: POST /cluster/sdn/reload to activate.
        # TODO: walk self._vm_entries (vm_name, mac, ip) from
        # register_vm() and create IPAM reservations pinning each MAC
        # to its intended IP.
        raise NotImplementedError(
            "ProxmoxVirtualNetwork.start is not yet implemented — "
            "see the testrange.backends.proxmox.network docstring."
        )

    def stop(self, context: AbstractOrchestrator) -> None:
        # TODO: delete IPAM reservations for self._vm_entries.
        # TODO: DELETE /cluster/sdn/vnets/{name} and
        # /cluster/sdn/subnets/{name}.
        # TODO: POST /cluster/sdn/reload.
        raise NotImplementedError(
            "ProxmoxVirtualNetwork.stop is not yet implemented."
        )

    def backend_name(self) -> str:
        # TODO: Proxmox caps SDN vnet names at 8 chars; the current
        # libvirt scheme (``tr-<net>-<run>``, 15 chars) needs
        # tightening.  For now, surface a clear error so nobody
        # relies on a stubbed name.
        raise NotImplementedError(
            "ProxmoxVirtualNetwork.backend_name is not yet implemented."
        )

    # Required stub methods (register_vm, bind_run, etc.) are
    # inherited from AbstractVirtualNetwork's helper properties or
    # will be added here alongside the REST integration.
