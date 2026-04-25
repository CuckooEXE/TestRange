"""Proxmox VE virtual network (SDN-backed).

Maps a TestRange :class:`~testrange.networks.base.AbstractVirtualNetwork`
onto an SDN vnet under the orchestrator's simple-zone (created on
``__enter__``).  The vnet carries a single subnet whose CIDR matches
the user-supplied :attr:`subnet`.

Static IPs vs. SDN DHCP
-----------------------

PVE SDN can serve DHCP off a subnet via its ``dhcp-range`` and
``dhcp-dns-server`` fields, which feed an IPAM database that tracks
allocations.  We don't use that yet — TestRange VMs that need a fixed
address get one through their own config path (cloud-init
``network-config``, ``answer.toml`` ``[network]`` block, autounattend
NIC settings) so we sidestep the IPAM reservation dance entirely.
:meth:`register_vm` records the ``(name, mac, ip)`` tuple so a future
slice can flip on IPAM-backed reservations without rewriting the
caller surface.

PVE name length cap
-------------------

PVE caps SDN vnet names at 8 characters.  We synthesise the name as
``<network-name[:4]><run-id[:4]>`` so concurrent runs don't collide
on the same logical network name.  The ``-`` separator the libvirt
backend uses doesn't fit; the four-and-four format is the longest we
can get away with.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from testrange._logging import get_logger
from testrange.exceptions import NetworkError
from testrange.networks.base import AbstractVirtualNetwork

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator

_log = get_logger(__name__)

_MAX_VNET_NAME_LEN = 8
"""PVE caps SDN vnet names at 8 characters."""


def _proxmox_client(context: AbstractOrchestrator) -> Any:
    """Pull the proxmoxer client off a Proxmox orchestrator."""
    return context._client  # type: ignore[attr-defined]


def _proxmox_zone(context: AbstractOrchestrator) -> str:
    """Pull the SDN zone name off a Proxmox orchestrator."""
    return context._zone  # type: ignore[attr-defined]


def _mac_for_vm_network(vm_name: str, net_name: str) -> str:
    """Generate a deterministic MAC address for a VM/network pair.

    Same scheme as :func:`testrange.backends.libvirt.network._mac_for_vm_network`
    so a VM that lands on either backend gets the same MAC, keeping
    cloud-init / answer.toml network configs portable.  Re-implemented
    here (rather than imported) so the proxmox backend doesn't reach
    into a sibling backend's private name.
    """
    import hashlib

    digest = hashlib.sha256(f"{vm_name}:{net_name}".encode()).digest()
    b = bytearray(6)
    b[0], b[1], b[2] = 0x52, 0x54, 0x00
    b[3], b[4], b[5] = digest[0], digest[1], digest[2]
    return ":".join(f"{x:02x}" for x in b)


class ProxmoxVirtualNetwork(AbstractVirtualNetwork):
    """Proxmox VE SDN-backed virtual network.

    On :meth:`start` the orchestrator creates an SDN vnet under the
    shared simple-zone, then a subnet whose CIDR matches
    :attr:`subnet`.  :meth:`stop` deletes both and reloads SDN.

    :param name: Logical network name (used by ``VirtualNetworkRef``
        matching, NIC bridge selection, and the deterministic MAC
        scheme).  Mapped to a PVE-legal 8-character vnet name via
        :meth:`backend_name`.
    :param subnet: CIDR (e.g. ``'10.0.50.0/24'``).  Becomes the SDN
        subnet's CIDR.
    :param dhcp: Reserved for future SDN-DHCP support; currently
        ignored — see the module docstring.
    :param internet: If ``True``, the subnet is created with
        ``snat=1`` so PVE installs an IP-masquerade rule and guests
        can reach the host's upstream network.  If ``False``, the
        vnet is fully isolated.
    :param dns: Reserved for future SDN-DNS support; currently
        ignored.
    """

    _run_id: str | None
    _vm_entries: list[tuple[str, str, str]]
    _vnet_name: str | None
    _subnet_id: str | None
    _client: Any

    def __init__(
        self,
        name: str,
        subnet: str,
        dhcp: bool = True,
        internet: bool = False,
        dns: bool = True,
    ) -> None:
        super().__init__(name, subnet, dhcp, internet, dns)
        self._run_id = None
        self._vm_entries = []
        self._vnet_name = None
        self._subnet_id = None
        self._client = None

    def bind_run(self, run_id: str) -> None:
        """Associate this network with a specific run ID.

        Called by :class:`ProxmoxOrchestrator` before :meth:`start`.
        Also clears any VM registrations from a previous run so the
        same instance can be re-entered without accumulating stale
        state.

        :param run_id: UUID string for the current test run.
        """
        self._run_id = run_id
        self._vm_entries.clear()

    def register_vm(self, vm_name: str, ip: str) -> str:
        """Register a VM's IP and return its deterministic MAC address.

        The MAC is derived from ``(vm_name, self.name)`` — same scheme
        as the libvirt backend, so the cloud-init / answer.toml
        network configs the builders generate are portable across
        backends.

        :param vm_name: VM name.
        :param ip: IP address to assign (consumed by the VM-level
            cloud-init / answer.toml NIC config; SDN-IPAM reservation
            will use this in a future slice).
        :returns: MAC address string.
        """
        mac = _mac_for_vm_network(vm_name, self.name)
        self._vm_entries.append((vm_name, mac, ip))
        return mac

    def register_vm_with_mac(self, vm_name: str, mac: str, ip: str) -> None:
        """Register a VM with an externally-computed MAC address."""
        self._vm_entries.append((vm_name, mac, ip))

    def backend_name(self) -> str:
        """Return the PVE-legal SDN vnet name (≤ 8 characters).

        Format: ``<net[:4]><run[:4]>``.  Both halves are sanitised to
        lowercase ASCII alphanumerics — PVE rejects ``-`` and ``_`` in
        SDN IDs.

        :returns: vnet name string.
        :raises RuntimeError: If :meth:`bind_run` has not been called.
        """
        if self._run_id is None:
            raise RuntimeError(
                "bind_run() must be called before backend_name()"
            )
        prefix = re.sub(r"[^a-z0-9]", "", self.name.lower())[:4]
        suffix = re.sub(r"[^a-z0-9]", "", self._run_id.lower())[:4]
        # Pad short prefixes / suffixes so the rendered name has a
        # stable shape even when callers use very short net names.
        prefix = (prefix or "net").ljust(4, "0")[:4]
        suffix = suffix.ljust(4, "0")[:4]
        return f"{prefix}{suffix}"[:_MAX_VNET_NAME_LEN]

    def start(self, context: AbstractOrchestrator) -> None:
        """Create the SDN vnet + subnet and reload SDN.

        :param context: The :class:`ProxmoxOrchestrator` driving this
            run.  Its proxmoxer client and zone name are pulled off
            it.
        :raises NetworkError: If the create / reload calls fail.
        """
        client = _proxmox_client(context)
        zone = _proxmox_zone(context)
        vnet = self.backend_name()

        try:
            client.cluster.sdn.vnets.post(vnet=vnet, zone=zone)

            subnet_params: dict[str, Any] = {
                "type": "subnet",
                "subnet": self.subnet,
                "gateway": self.gateway_ip,
            }
            if self.internet:
                # PVE installs an IP-masquerade rule on the host's
                # upstream interface so guests can reach the outer
                # network.  Without ``snat=1`` the vnet is reachable
                # only from sibling guests on the same vnet.
                subnet_params["snat"] = 1

            client.cluster.sdn.vnets(vnet).subnets.post(**subnet_params)

            # PVE auto-derives the subnet ID from ``<zone>-<cidr>``
            # with the slash replaced by a dash.  Capture it from a
            # listing rather than computing the format ourselves —
            # PVE has changed the encoding once already across major
            # versions.
            self._subnet_id = self._lookup_subnet_id(client, vnet)

            # Apply pending SDN config — without this, the vnet sits
            # in a "pending" state and isn't actually attached to any
            # bridge.
            client.cluster.sdn.put()

        except Exception as exc:
            # Best-effort rollback so a failed start doesn't leak a
            # half-created vnet.
            self._cleanup(client, vnet, self._subnet_id, log_failures=False)
            raise NetworkError(
                f"Failed to start SDN vnet {vnet!r}: {exc}"
            ) from exc

        self._client = client
        self._vnet_name = vnet
        _log.debug(
            "vnet %r active: zone=%s subnet=%s gateway=%s internet=%s",
            vnet, zone, self.subnet, self.gateway_ip, self.internet,
        )

    def stop(self, context: AbstractOrchestrator) -> None:
        """Delete the SDN vnet + subnet and reload SDN.

        Best-effort: never raises.  Safe to call if :meth:`start` was
        not (or only partially) successful.

        :param context: The :class:`ProxmoxOrchestrator`.
        """
        client = _proxmox_client(context)
        if self._vnet_name is None:
            return
        self._cleanup(
            client, self._vnet_name, self._subnet_id, log_failures=True,
        )
        self._vnet_name = None
        self._subnet_id = None
        self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _lookup_subnet_id(client: Any, vnet: str) -> str:
        """Return the auto-assigned subnet ID for *vnet*'s only subnet.

        PVE auto-names subnets ``<zone>-<cidr-with-/-replaced>`` but
        the exact encoding has shifted across versions.  Listing and
        picking the first subnet is more robust than recomputing.
        """
        subnets = client.cluster.sdn.vnets(vnet).subnets.get()
        if not subnets:
            raise NetworkError(
                f"vnet {vnet!r} has no subnets after create — "
                "PVE may have rejected the subnet silently"
            )
        return subnets[0]["subnet"]

    @staticmethod
    def _cleanup(
        client: Any,
        vnet: str,
        subnet_id: str | None,
        *,
        log_failures: bool,
    ) -> None:
        """Tear down a vnet + its subnets and reload SDN.

        Each step is independent and best-effort so a partial state
        (vnet without subnet, subnet without vnet) still gets cleaned
        up.
        """
        if subnet_id is not None:
            try:
                client.cluster.sdn.vnets(vnet).subnets(subnet_id).delete()
            except Exception as exc:
                if log_failures:
                    _log.warning(
                        "failed to delete subnet %s/%s: %s",
                        vnet, subnet_id, exc,
                    )
        try:
            client.cluster.sdn.vnets(vnet).delete()
        except Exception as exc:
            if log_failures:
                _log.warning("failed to delete vnet %s: %s", vnet, exc)
        try:
            client.cluster.sdn.put()
        except Exception as exc:
            if log_failures:
                _log.warning("failed to reload SDN after cleanup: %s", exc)
