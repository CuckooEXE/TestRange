"""L2 fabric for the Proxmox backend — per-run SDN zone + per-switch vnet.

Each test run gets its **own** ``simple`` SDN zone (``tr<hex>``, 8 chars — PVE's
SDN-id limit), minted once on the :class:`ProxmoxDriver` instance (one driver ==
one run) and passed in here. It is created on the first ``create_switch`` and
torn down when the run's last switch goes. One vnet per Switch lives in that
zone; all Networks on a Switch share the vnet (one wire, multiple labels). SDN
config is staged then applied cluster-wide with a single ``PUT /cluster/sdn`` —
vnets/zones are inert until applied.

The zone is **not** an author knob and is never recomputed: teardown is
self-discovering — ``destroy_switch`` reads a vnet's ``zone`` before deleting
it, then drops the zone once it holds no vnets — so a crash-recovery driver
rebuilt ``from_uri`` (a fresh instance with a *different* minted zone, and which
never saw ``run_id``) still cleans the right zone.

The isolated guest segment is always the per-Switch SDN vnet. For an
``uplink``+``nat`` Switch the sidecar's ``eth1`` needs a segment with upstream
connectivity to MASQUERADE out of; on Proxmox that is an **existing host
bridge** (e.g. ``vmbr0``, the one carrying the default gateway). ``switch.uplink``
is a logical name (ADR-0016) the driver resolves against the profile's
``[uplinks]`` map to that bridge name (``resolved_uplink``); the bridge itself is
static, operator-owned, out-of-band config — the driver does not create, SNAT,
fence, or destroy it. It just hands the resolved bridge name back so the
orchestrator wires the sidecar's ``eth1`` onto it. Preflight verifies both that
the name is mapped and that the bridge exists.

A ``mgmt=True`` Switch additionally gets the host's ``.2`` mgmt adapter on the
vnet (ADR-0009 option B): an SDN **subnet** on the vnet carrying
``gateway = switch.mgmt_ip``, which PVE plumbs onto the vnet bridge on the
(single, pinned) node — the Proxmox analog of libvirt's ``<ip address=.2>``. The
subnet sets no SNAT and no DHCP IPAM, so ``.2`` is a pure host adapter that
coexists with the sidecar at ``.1``; reachability is a hypervisor-local
guarantee (guests ↔ host), not a remote-test-runner one. The subnet is torn
down before its vnet in ``destroy_switch`` (self-discovering, so a ``from_uri``
teardown driver needs no extra state).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.proxmox._naming import vnet_id

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.proxmox._client import ProxmoxClient
    from testrange.networks.base import Network, Switch

_log = get_logger(__name__)


def _apply(client: ProxmoxClient) -> None:
    """Apply staged SDN config cluster-wide and wait for the reload task."""
    result = client.api.cluster.sdn.put()
    if isinstance(result, str) and result.startswith("UPID:"):
        client.wait_task(result)


def _ensure_zone(client: ProxmoxClient, zone: str) -> None:
    zones = {z["zone"] for z in client.api.cluster.sdn.zones.get()}
    if zone not in zones:
        client.api.cluster.sdn.zones.post(type="simple", zone=zone)
        _log.info("created per-run SDN simple zone %s", zone)


def _ensure_mgmt_subnet(client: ProxmoxClient, vid: str, switch: Switch) -> None:
    """Realize ``Switch(mgmt=True)`` as the host's ``.2`` adapter on the vnet.

    ADR-0009 (B): a subnet on the vnet with ``gateway = switch.mgmt_ip`` and no
    SNAT/DHCP makes PVE assign that IP to the vnet bridge on the node — a plain
    host L2 presence, coexisting with the sidecar's ``.1``. Idempotent: a subnet
    for this CIDR already present (a re-entrant ``create_switch``) is left alone.
    """
    cidr = str(switch.network)
    present = {s.get("cidr") for s in client.api.cluster.sdn.vnets(vid).subnets.get()}
    if cidr in present:
        return
    client.api.cluster.sdn.vnets(vid).subnets.post(
        subnet=cidr, type="subnet", gateway=switch.mgmt_ip
    )
    _log.info("added mgmt subnet %s (gw %s) on vnet %s", cidr, switch.mgmt_ip, vid)


def create_switch(
    client: ProxmoxClient,
    zone: str,
    switch: Switch,
    backend_name: str,
    *,
    resolved_uplink: str | None = None,
) -> str | None:
    vid = vnet_id(backend_name)
    _ensure_zone(client, zone)
    existing = {v["vnet"] for v in client.api.cluster.sdn.vnets.get()}
    if vid not in existing:
        client.api.cluster.sdn.vnets.post(vnet=vid, zone=zone, alias=backend_name)

    if switch.mgmt:
        _ensure_mgmt_subnet(client, vid, switch)

    _apply(client)
    _log.info("created SDN vnet %s (switch %s) in zone %s", vid, backend_name, zone)
    # For uplink+nat, hand back the existing host bridge (``resolved_uplink`` —
    # the iface the profile's [uplinks] mapped switch.uplink to) so the
    # orchestrator attaches the sidecar's eth1 to it; the sidecar then
    # MASQUERADEs the isolated vnet out through it. Egress is out-of-band: the
    # bridge is static operator config — not created here, not torn down in
    # destroy_switch. A bare/isolated switch has no uplink segment.
    if switch.uplink is not None and switch.sidecar is not None and switch.sidecar.nat:
        return resolved_uplink
    return None


def destroy_switch(client: ProxmoxClient, backend_name: str) -> None:
    vid = vnet_id(backend_name)
    vnets = client.api.cluster.sdn.vnets.get()
    by_id = {v["vnet"]: v for v in vnets}
    target = by_id.get(vid)
    if target is None:
        return
    zone = target.get("zone")
    # Drop any subnets first (a mgmt Switch carries one; PVE refuses to delete a
    # vnet that still holds subnets). Self-discovering off the live list, so a
    # from_uri teardown driver cleans them without knowing the Switch was mgmt.
    for sub in client.api.cluster.sdn.vnets(vid).subnets.get():
        client.api.cluster.sdn.vnets(vid).subnets(sub["subnet"]).delete()
    client.api.cluster.sdn.vnets(vid).delete()
    # Drop the per-run zone once it holds no more vnets (self-discovering, so a
    # from_uri-rebuilt teardown driver needs no run_id). Decide against a *fresh*
    # vnet list, not the pre-delete one: re-fetching means we never delete a zone
    # out from under a vnet created since the first read, nor leak one whose last
    # sibling vanished meanwhile. (Single-instance today — ADR-0018 — but the
    # re-fetch is cheap and keeps the GC correct if that ever changes.)
    remaining = client.api.cluster.sdn.vnets.get()
    if zone is not None and not any(v.get("zone") == zone for v in remaining):
        client.api.cluster.sdn.zones(zone).delete()
        _log.info("destroyed per-run SDN zone %s (last vnet removed)", zone)
    _apply(client)
    _log.info("destroyed SDN vnet %s (switch %s)", vid, backend_name)


def create_network(
    client: ProxmoxClient,
    network: Network,
    switch: Switch,
    backend_name: str,
    *,
    switch_backend_name: str,
) -> str:
    """Attach a Network to a switch's vnet.

    Networks share the switch's single vnet, so this resolves to that vnet's id
    and records nothing new on the backend — the vnet was created in
    ``create_switch`` and is torn down in ``destroy_switch``.
    """
    del client, network, switch, backend_name
    return vnet_id(switch_backend_name)
