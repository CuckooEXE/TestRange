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

    :param name: Logical network name (used by ``vNIC``
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

    _vnet_created: bool
    """``True`` once :meth:`start` has successfully created the vnet
    (i.e. PVE accepted the POST).  Drives :meth:`stop` so we only
    delete things this instance actually created — important on the
    rollback path when ``vnets.post`` fails because the name is
    already taken by another run."""

    _subnet_created: bool
    """``True`` once :meth:`start` has successfully created the
    subnet under our vnet."""

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
        self._vnet_created = False
        self._subnet_created = False

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

        # Track creation state explicitly so the rollback path only
        # undoes work *this* call did.  Without this, a name-collision
        # failure on ``vnets.post`` would lead the rollback to find the
        # other run's vnet by name and (try to) delete it.
        self._vnet_name = vnet
        self._client = client

        try:
            client.cluster.sdn.vnets.post(vnet=vnet, zone=zone)
            self._vnet_created = True

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
            self._subnet_created = True

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
            # Rollback only the parts this call created.
            self._cleanup(
                client,
                vnet=vnet if self._vnet_created else None,
                subnet_id=self._subnet_id if self._subnet_created else None,
            )
            self._vnet_created = False
            self._subnet_created = False
            self._vnet_name = None
            self._subnet_id = None
            self._client = None
            raise NetworkError(
                f"Failed to start SDN vnet {vnet!r}: {exc}"
            ) from exc

        _log.debug(
            "vnet %r active: zone=%s subnet=%s gateway=%s internet=%s",
            vnet, zone, self.subnet, self.gateway_ip, self.internet,
        )

    def stop(self, context: AbstractOrchestrator) -> None:
        """Delete the SDN vnet + subnet and reload SDN.

        Never raises — :meth:`AbstractOrchestrator.__exit__` cannot
        let teardown errors mask the original exception that ended
        the ``with`` block.  Errors are logged at WARNING level
        instead, except for 404s on the delete calls (which mean the
        resource was already gone — that's :meth:`stop`'s
        postcondition met).  Safe to call before :meth:`start` or
        after a partial failure.

        :param context: The :class:`ProxmoxOrchestrator`.
        """
        client = _proxmox_client(context)
        if not self._vnet_created:
            # Either start() never ran, or it failed before creating
            # the vnet.  Either way nothing to clean up here.
            return
        self._cleanup(
            client,
            vnet=self._vnet_name,
            subnet_id=self._subnet_id if self._subnet_created else None,
        )
        self._vnet_name = None
        self._subnet_id = None
        self._client = None
        self._vnet_created = False
        self._subnet_created = False

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
        vnet: str | None,
        subnet_id: str | None,
    ) -> None:
        """Tear down a vnet + its subnets and reload SDN.

        Both args are optional so the rollback path in :meth:`start`
        can ask only for the parts it actually created — passing
        ``vnet=None`` if PVE refused the ``vnets.post``, or
        ``subnet_id=None`` if the subnet was never created.

        Look-before-you-leap: list the vnets / subnets first and
        only DELETE things that actually exist (and that we
        recorded creating).  PVE returns HTTP 500 ("does not
        exist") rather than 404 for missing SDN resources, so we
        can't disambiguate "already gone" from "actually broken"
        by status code alone — listing first avoids the 500-spam
        entirely and keeps logged warnings meaningful (a warning
        here means the resource exists *and* we couldn't delete
        it).

        Errors that do happen are logged but never re-raised —
        this helper feeds both the orchestrator-teardown path
        (where raising would mask the user's exception) and the
        ``start()`` rollback path (where raising would mask the
        original create failure).
        """
        if vnet is None:
            return  # nothing to do
        # 1. Check what exists.  If we can't even list, bail —
        # without a current view we'd be guessing whether each
        # delete failure is "already gone" or "real problem", and
        # that's exactly the noise we're trying to avoid.
        try:
            vnet_names = {v["vnet"] for v in client.cluster.sdn.vnets.get()}
        except Exception as exc:
            _log.warning(
                "failed to list SDN vnets for cleanup of %r: %s — "
                "skipping further teardown to avoid spurious errors",
                vnet, exc,
            )
            return
        if vnet not in vnet_names:
            return  # already gone — nothing to do

        # 2. Subnet, if we recorded one.
        if subnet_id is not None:
            try:
                subnet_ids = {
                    s["subnet"]
                    for s in client.cluster.sdn.vnets(vnet).subnets.get()
                }
            except Exception as exc:
                _log.warning(
                    "failed to list subnets of vnet %r: %s",
                    vnet, exc,
                )
                subnet_ids = set()
            if subnet_id in subnet_ids:
                ProxmoxVirtualNetwork._call_and_log(
                    f"delete subnet {vnet}/{subnet_id}",
                    lambda: client.cluster.sdn.vnets(vnet)
                        .subnets(subnet_id).delete(),
                )

        # 3. The vnet itself.
        ProxmoxVirtualNetwork._call_and_log(
            f"delete vnet {vnet}",
            lambda: client.cluster.sdn.vnets(vnet).delete(),
        )

        # 4. Apply pending SDN config so the deletes actually take
        # effect.  Without this PUT, the entries hang around as
        # "pending deletion" until the next reload from anywhere.
        ProxmoxVirtualNetwork._call_and_log(
            "reload SDN after cleanup",
            lambda: client.cluster.sdn.put(),
        )

    @staticmethod
    def _call_and_log(action: str, fn: Any) -> None:
        """Run *fn* and log any failure at WARNING level.

        Used after we've already confirmed the resource exists, so
        any failure here is a real problem worth surfacing — the
        log line includes the HTTP status code when proxmoxer
        attached one.
        """
        try:
            fn()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is not None:
                _log.warning("failed to %s: HTTP %s — %s", action, status, exc)
            else:
                _log.warning("failed to %s: %s", action, exc)
