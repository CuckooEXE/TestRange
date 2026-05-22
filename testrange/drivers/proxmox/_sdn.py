"""L2 fabric for the Proxmox backend — per-run SDN zone + per-switch vnet.

Each test run gets its **own** ``simple`` SDN zone (``z<run-hash>``), created on
the first ``create_switch`` and torn down when the run's last switch goes. One
vnet per Switch lives in that zone; all Networks on a Switch share the vnet (one
wire, multiple labels). SDN config is staged then applied cluster-wide with a
single ``PUT /cluster/sdn`` — vnets/zones are inert until applied.

Teardown is self-discovering: ``destroy_switch`` reads a vnet's ``zone`` before
deleting it, then drops the zone once it holds no vnets — so a crash-recovery
driver rebuilt ``from_uri`` (which never saw ``run_id``) still cleans the zone.

v1 realises **isolated** switches only. ``uplink``+``nat`` is rejected at
preflight, so ``create_switch`` never returns an uplink segment — always ``None``.
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


def create_switch(
    client: ProxmoxClient, zone: str, switch: Switch, backend_name: str
) -> str | None:
    vid = vnet_id(backend_name)
    _ensure_zone(client, zone)
    existing = {v["vnet"] for v in client.api.cluster.sdn.vnets.get()}
    if vid not in existing:
        client.api.cluster.sdn.vnets.post(vnet=vid, zone=zone, alias=backend_name)
    _apply(client)
    _log.info("created SDN vnet %s (switch %s) in zone %s", vid, backend_name, zone)
    # v1: isolated only. uplink+nat is rejected in preflight, so there is never
    # an uplink-facing segment to hand back for the sidecar's eth1.
    return None


def destroy_switch(client: ProxmoxClient, backend_name: str) -> None:
    vid = vnet_id(backend_name)
    vnets = client.api.cluster.sdn.vnets.get()
    by_id = {v["vnet"]: v for v in vnets}
    target = by_id.get(vid)
    if target is None:
        return
    zone = target.get("zone")
    client.api.cluster.sdn.vnets(vid).delete()
    # Drop the per-run zone once it holds no more vnets (self-discovering, so a
    # from_uri-rebuilt teardown driver needs no run_id).
    if zone is not None and not any(v.get("zone") == zone and v["vnet"] != vid for v in vnets):
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
