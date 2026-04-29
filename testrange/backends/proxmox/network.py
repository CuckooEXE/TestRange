"""Proxmox VE virtual network (SDN-backed).

Maps a TestRange :class:`~testrange.networks.base.AbstractVirtualNetwork`
onto an SDN vnet under the orchestrator's simple-zone (created on
``__enter__``).  The vnet carries a single subnet whose CIDR matches
the user-supplied :attr:`subnet`.

DHCP + DNS via PVE SDN dnsmasq
------------------------------

The TestRange-created SDN zone ships with ``dhcp = "dnsmasq"`` set
(at *zone* scope per the PVE 9.x schema — putting it on the subnet
POST is a 400 ``"property is not defined in schema"``).  That tells
PVE to spin up a per-vnet ``dnsmasq`` instance for every subnet
under the zone, bound to the vnet's bridge interface.  Each subnet
carries a ``dhcp-range`` defining the lease pool.  Each VM that
calls :meth:`register_vm` also lands in the SDN IPAM (``POST
/cluster/sdn/vnets/{vnet}/ips``) with ``(mac, ip,
hostname=<vm>.<vnet>)``.  PVE turns those IPAM entries into
``dhcp-host=...`` directives for the dnsmasq config it generates, so:

* DHCP requests from registered MACs receive their reserved IP
  (deterministic for tests; same address every run).
* dnsmasq's DNS service resolves ``<vm>.<vnet>`` to the reserved IP
  for any querier on the vnet — libvirt-style cross-VM hostname
  resolution.
* The vnet's gateway is dnsmasq itself (via DHCP option 6), so VMs
  inherit DNS without further configuration.

Cloud-init / answer.toml still emit a static-IP block matching the
IPAM-reserved address (so VMs come up immediately without a DHCP
boot delay), but the dnsmasq lease tracking ensures the same IP is
served for any subsequent reboot or rebuild that takes the DHCP
path.

The ``dnsmasq`` package is required on every PVE node TestRange
talks to.  :meth:`ProxmoxOrchestrator._preflight_dnsmasq_installed`
checks for it on every ``__enter__``; the
:meth:`~testrange.backends.proxmox.ProxmoxOrchestrator.prepare_outer_vm`
hook injects ``Apt("dnsmasq")`` into the package list of any
:class:`~testrange.Hypervisor` whose inner orchestrator is
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator`, so nested
PVE-on-libvirt setups satisfy the dependency by construction.

PVE name length cap
-------------------

PVE caps SDN vnet names at 8 characters.  We synthesise the name as
``<network-name[:4]><run-id[:4]>`` so concurrent runs don't collide
on the same logical network name.  The ``-`` separator the libvirt
backend uses doesn't fit; the four-and-four format is the longest we
can get away with.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from testrange._logging import get_logger
from testrange.exceptions import NetworkError
from testrange.networks.base import AbstractSwitch, AbstractVirtualNetwork

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator

_log = get_logger(__name__)

_DHCP_RANGE_RESERVED_HEAD = 10
"""Number of low-end host addresses excluded from the dynamic
``dhcp-range`` so static MAC reservations have a stable slice to live
in.  ``.1`` is the gateway; ``.2``–``.10`` (with the default head of
10) carry IPAM reservations TestRange writes for each registered VM.
``.11`` onward is the dynamic range dnsmasq hands out to anything not
already reserved (rare in TestRange, but covers
hand-attached debug VMs).  Subnets too small to honour this split
(``/30`` and below) raise :class:`NetworkError` at start time."""

_MAX_VNET_NAME_LEN = 8
"""PVE caps SDN vnet names at 8 characters."""

_MAX_ZONE_NAME_LEN = 8
"""PVE caps SDN zone IDs at 8 characters of lowercase ASCII alphanum."""

_VALID_ZONE_TYPES = frozenset({"simple", "vlan", "qinq", "vxlan", "evpn"})
"""SDN zone types PVE accepts in its ``POST /cluster/sdn/zones`` endpoint.

We accept the strings PVE does verbatim; backend-specific extras
(VXLAN VRF / EVPN AS / ...) flow through ``zone_extra`` on
:class:`ProxmoxSwitch`."""


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


def _zone_id(name: str) -> str:
    """Sanitise *name* into a PVE-legal SDN zone ID.

    PVE rejects ``-`` / ``_`` and is case-sensitive; cap at
    :data:`_MAX_ZONE_NAME_LEN` characters of lowercase ASCII
    alphanumerics.  Empty input falls back to ``"sw"`` so the
    rendered ID always has a stable shape.
    """
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    return (cleaned or "sw")[:_MAX_ZONE_NAME_LEN]


class ProxmoxSwitch(AbstractSwitch):
    """Proxmox VE SDN-zone-backed switch.

    Maps an :class:`AbstractSwitch` onto an SDN zone created via
    ``POST /cluster/sdn/zones``.  Each :class:`ProxmoxVirtualNetwork`
    that names this switch (or that picks it up as the
    orchestrator's default) lives as a vnet inside the zone.

    :param name: Logical switch name.  PVE-mangled to
        :data:`_MAX_ZONE_NAME_LEN` chars of lowercase ASCII
        alphanumerics for the SDN zone ID — see :meth:`backend_name`.
    :param switch_type: PVE SDN zone type.  One of
        :data:`_VALID_ZONE_TYPES` (``"simple"`` / ``"vlan"`` /
        ``"qinq"`` / ``"vxlan"`` / ``"evpn"``).  Defaults to
        ``"simple"`` — the type TestRange uses for its own
        infrastructure zone.  Most users want this default; pick
        ``"vlan"`` / ``"vxlan"`` only if you need real L2 isolation
        on a physical fabric.
    :param uplinks: Physical NIC name(s) on the PVE node to use as
        the zone's uplink.  Required for ``"vlan"`` / ``"qinq"`` /
        ``"vxlan"`` zones; ignored for ``"simple"``.  PVE's REST
        API takes a single ``bridge=`` parameter for VLAN-shaped
        zones — we send the first uplink and warn if more were
        declared (PVE doesn't model uplink teaming inside a zone;
        team it at the host network layer instead).
    :param zone_extra: Free-form ``dict`` merged into the
        ``POST /cluster/sdn/zones`` body.  Use it for VXLAN/EVPN
        knobs (``peers``, ``vrf-vxlan``, ``controller``, …) that
        TestRange doesn't model first-class.
    :param mtu: Optional MTU for the zone.  Forwarded as PVE's
        ``mtu`` field; ``None`` means "leave PVE's default".
    """

    _client: Any
    _zone_id: str | None
    _zone_created: bool
    """``True`` once :meth:`start` has successfully created the zone.
    Drives :meth:`stop` so we only delete what *this* call created —
    important when the zone already existed from a prior run, in
    which case we don't want to tear out an in-use shared zone on
    teardown."""

    def __init__(
        self,
        name: str,
        switch_type: str | None = None,
        uplinks: Sequence[str] | None = None,
        *,
        zone_extra: dict[str, Any] | None = None,
        mtu: int | None = None,
    ) -> None:
        # Default zone type is "simple" — matches the existing
        # TestRange install zone behaviour and works without
        # uplinks.
        resolved_type = switch_type or "simple"
        if resolved_type not in _VALID_ZONE_TYPES:
            raise NetworkError(
                f"ProxmoxSwitch {name!r}: switch_type "
                f"{resolved_type!r} is not one of "
                f"{sorted(_VALID_ZONE_TYPES)}"
            )
        super().__init__(
            name=name, switch_type=resolved_type, uplinks=uplinks,
        )
        self.zone_extra = dict(zone_extra) if zone_extra else {}
        self.mtu = mtu
        self._client = None
        self._zone_id = None
        self._zone_created = False

    def backend_name(self) -> str:
        """Return the PVE SDN zone ID for this switch."""
        return _zone_id(self.name)

    def start(self, context: AbstractOrchestrator) -> None:
        """Create the SDN zone (idempotent) and apply pending SDN config.

        :raises NetworkError: If the create call fails.
        """
        client = _proxmox_client(context)
        zone_id = self.backend_name()
        self._client = client
        self._zone_id = zone_id

        # Idempotent: a zone we already brought up (or one a prior
        # run left behind that names the same logical switch) gets
        # reused as-is.  Don't set ``_zone_created`` in that case
        # so teardown leaves it alone.
        try:
            existing = client.cluster.sdn.zones.get()
        except Exception as exc:
            raise NetworkError(
                f"ProxmoxSwitch {self.name!r}: cannot list SDN zones: "
                f"{exc}"
            ) from exc
        if any(z.get("zone") == zone_id for z in (existing or [])):
            _log.debug(
                "switch %r: SDN zone %s already present — reusing",
                self.name, zone_id,
            )
            return

        params: dict[str, Any] = {
            "type": self.switch_type,
            "zone": zone_id,
            # ``dhcp`` lives at zone scope per the PVE 9.x SDN
            # schema (subnets only carry ``dhcp-range`` /
            # ``dhcp-dns-server``); set it here so every vnet in
            # this user-defined zone gets the per-subnet dnsmasq
            # behaviour TestRange relies on, same as the default
            # zone created by ``ProxmoxOrchestrator._ensure_sdn_zone``.
            "dhcp": "dnsmasq",
        }
        if self.uplinks:
            # PVE's VLAN/QinQ zones take ``bridge=`` (a single host
            # bridge name) as the underlying L2.  VXLAN takes
            # ``peers=`` and a different shape — pass the uplink
            # through ``zone_extra`` for those cases.  Warn loudly
            # if multiple uplinks were declared since PVE only
            # consumes one here.
            if self.switch_type in ("vlan", "qinq"):
                params["bridge"] = self.uplinks[0]
                if len(self.uplinks) > 1:
                    _log.warning(
                        "switch %r: %d uplinks declared but PVE %s "
                        "zones only accept one bridge — using %r; "
                        "team additional NICs at the host network "
                        "layer instead.",
                        self.name, len(self.uplinks),
                        self.switch_type, self.uplinks[0],
                    )
        if self.mtu is not None:
            params["mtu"] = self.mtu
        params.update(self.zone_extra)

        _log.info(
            "creating SDN %s-zone %s (uplinks=%s)",
            self.switch_type, zone_id, self.uplinks or "—",
        )
        try:
            client.cluster.sdn.zones.post(**params)
            client.cluster.sdn.put()
        except Exception as exc:
            raise NetworkError(
                f"ProxmoxSwitch {self.name!r}: failed to create zone "
                f"{zone_id!r}: {exc}"
            ) from exc
        self._zone_created = True

    def stop(self, context: AbstractOrchestrator) -> None:
        """Delete the SDN zone if *we* created it.

        Best-effort: per-resource errors are logged and swallowed so
        teardown never raises.  No-op if the zone was already
        present at :meth:`start` time (we don't tear out a zone we
        didn't bring up).
        """
        del context  # client is stashed at start()
        if not self._zone_created or self._client is None or self._zone_id is None:
            return
        try:
            self._client.cluster.sdn.zones(self._zone_id).delete()
            self._client.cluster.sdn.put()
        except Exception as exc:
            _log.warning(
                "switch %r: failed to delete SDN zone %s: %s — "
                "may need ``pvesh delete /cluster/sdn/zones/%s`` by "
                "hand",
                self.name, self._zone_id, exc, self._zone_id,
            )
        self._zone_created = False


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
        switch: AbstractSwitch | str | None = None,
    ) -> None:
        super().__init__(
            name, subnet, dhcp, internet, dns, switch=switch,
        )
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

    def _resolve_zone(self, context: AbstractOrchestrator) -> str:
        """Return the SDN zone ID this vnet should live in.

        Four cases:

        * :attr:`switch` is a :class:`ProxmoxSwitch` instance →
          use that switch's :meth:`backend_name` directly.
        * :attr:`switch` is any other :class:`AbstractSwitch`
          instance → look up its name on the orchestrator's
          promoted ``_switches`` list.  This is the common path
          when the user passed a generic :class:`Switch` to both
          the orchestrator's ``switches=`` and the network's
          ``switch=``: the orchestrator promoted the switch
          instance to a fresh :class:`ProxmoxSwitch`, but the
          network still holds the original reference.
        * :attr:`switch` is a string (logical switch name) → look
          up the matching :class:`ProxmoxSwitch` on the
          orchestrator's ``_switches`` and use its zone.
        * :attr:`switch` is ``None`` → fall back to the
          orchestrator's default zone (the pre-Switch behaviour
          where every vnet lived under one shared zone).
        """
        if isinstance(self.switch, ProxmoxSwitch):
            return self.switch.backend_name()
        if isinstance(self.switch, (AbstractSwitch, str)):
            wanted_name = (
                self.switch
                if isinstance(self.switch, str)
                else self.switch.name
            )
            switches: list[ProxmoxSwitch] = list(
                getattr(context, "_switches", []) or []
            )
            for sw in switches:
                if sw.name == wanted_name:
                    return sw.backend_name()
            raise NetworkError(
                f"VirtualNetwork {self.name!r}: switch "
                f"{wanted_name!r} not found in orchestrator's "
                f"switches list (available: "
                f"{[s.name for s in switches]})"
            )
        return _proxmox_zone(context)

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

        Zone selection: if :attr:`switch` is bound to a
        :class:`ProxmoxSwitch`, the vnet lands in *that* switch's
        zone.  Otherwise the orchestrator's default zone (set on
        ``__enter__``) is used — matches the pre-Switch behaviour
        where every vnet lived under one shared zone.

        :param context: The :class:`ProxmoxOrchestrator` driving this
            run.  Its proxmoxer client and (default) zone name are
            pulled off it.
        :raises NetworkError: If the create / reload calls fail.
        """
        client = _proxmox_client(context)
        zone = self._resolve_zone(context)
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

            dhcp_start, dhcp_end = self._dhcp_range()
            subnet_params: dict[str, Any] = {
                "type": "subnet",
                "subnet": self.subnet,
                "gateway": self.gateway_ip,
                # PVE 9.x SDN schema: the ``dhcp = "dnsmasq"``
                # selector lives at ZONE scope (set in
                # :meth:`ProxmoxOrchestrator._ensure_sdn_zone` and
                # :meth:`ProxmoxSwitch.start`); the subnet only
                # carries the lease range.  Putting ``dhcp`` on the
                # subnet POST is a 400 with "property is not defined
                # in schema and the schema does not allow additional
                # properties".
                "dhcp-range": [
                    f"start-address={dhcp_start},end-address={dhcp_end}",
                ],
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

            # Push every (mac, ip, hostname) tuple register_vm /
            # register_vm_with_mac collected before start() into PVE's
            # SDN IPAM.  PVE turns these into ``dhcp-host`` directives
            # in the auto-generated dnsmasq config, which gives us
            # deterministic DHCP leases AND FQDN DNS for ``<vm>.<vnet>``
            # (libvirt parity).  Done before the final ``put()`` so a
            # single SDN apply covers vnet + subnet + IPAM entries.
            self._push_ipam_entries(client)

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

    def _push_ipam_entries(self, client: Any) -> None:
        """POST each registered ``(mac, ip, hostname)`` to PVE's IPAM.

        Endpoint: ``POST /cluster/sdn/vnets/{vnet}/ips`` with
        ``mac`` + ``ip`` + ``hostname`` body.  PVE's pve-ipam plugin
        records the binding and the next SDN reload regenerates the
        per-vnet dnsmasq config with matching ``dhcp-host=...`` lines
        — that's what gives DHCP determinism and ``<vm>.<vnet>``
        DNS in one shot.

        Hostname format: ``<vm>.<vnet>`` (libvirt parity).  We FQDN-
        ify here rather than at :meth:`register_vm` time so callers
        keep seeing the simple two-arg signature; the FQDN only
        matters at the IPAM-push site.

        Errors during IPAM push surface as :class:`NetworkError` —
        if the IPAM POST fails the resulting dnsmasq config won't
        have our static reservations, and the VM will end up with a
        random dynamic-range lease that breaks every test that
        asserts a specific IP.  Loud is the right failure mode here.
        """
        if not self._vm_entries:
            return
        for vm_name, mac, ip in self._vm_entries:
            hostname = f"{vm_name}.{self.name}"
            try:
                client.cluster.sdn.vnets(self._vnet_name).ips.post(
                    mac=mac, ip=ip, hostname=hostname,
                )
            except Exception as exc:
                raise NetworkError(
                    f"VirtualNetwork {self.name!r}: failed to register "
                    f"IPAM entry mac={mac} ip={ip} hostname={hostname!r}: "
                    f"{exc}"
                ) from exc

    def _dhcp_range(self) -> tuple[str, str]:
        """Return ``(start, end)`` IPs for the SDN subnet's dnsmasq range.

        Reserves the first :data:`_DHCP_RANGE_RESERVED_HEAD` host
        addresses (``.1`` is the gateway, ``.2``–``.10`` carry IPAM
        reservations TestRange writes for each :meth:`register_vm`
        VM) so the dynamic range only fires for unregistered MACs.
        Subnets smaller than the reserved head + at least one dynamic
        slot raise :class:`NetworkError` rather than silently
        producing a bogus inverted range.
        """
        net = ipaddress.IPv4Network(self.subnet, strict=False)
        hosts = list(net.hosts())
        if len(hosts) < _DHCP_RANGE_RESERVED_HEAD + 1:
            raise NetworkError(
                f"VirtualNetwork {self.name!r}: subnet {self.subnet} is "
                f"too small ({len(hosts)} usable hosts) for the dnsmasq "
                f"reservation slice ({_DHCP_RANGE_RESERVED_HEAD} reserved "
                "for static IPAM entries + at least one dynamic).  Widen "
                "to /28 or larger."
            )
        start = hosts[_DHCP_RANGE_RESERVED_HEAD]
        end = hosts[-1]
        return str(start), str(end)

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
