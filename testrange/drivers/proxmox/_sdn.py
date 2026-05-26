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
bridge** (e.g. ``vmbr0``, the one carrying the default gateway), named by
``switch.uplink``. The driver does not create or destroy that bridge — it is
static, operator-owned config — it just hands the name back so the orchestrator
wires the sidecar's ``eth1`` onto it. So on Proxmox ``switch.uplink`` (including
the build switch's, resolved from the Hypervisor's ``build_switch``) names an
**existing bridge**, not a raw NIC (the generic "physical NIC" semantics in
PLAN §10 specialise per backend). Preflight verifies the bridge exists.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.proxmox._naming import egress_vnet_id, vnet_id

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.proxmox._client import ProxmoxClient
    from testrange.networks.base import ManagedEgress, Network, Switch

_log = get_logger(__name__)

# Private (RFC1918) ranges the managed-egress fence denies: the build network may
# reach the internet but not the host LAN, other SDN segments, or the mgmt net.
_RFC1918 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


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
    client: ProxmoxClient,
    zone: str,
    switch: Switch,
    backend_name: str,
    managed_egress: ManagedEgress | None = None,
) -> str | None:
    vid = vnet_id(backend_name)
    _ensure_zone(client, zone)
    existing = {v["vnet"] for v in client.api.cluster.sdn.vnets.get()}
    if vid not in existing:
        client.api.cluster.sdn.vnets.post(vnet=vid, zone=zone, alias=backend_name)

    if managed_egress is not None:
        # ManagedBuildSwitch (ADR-0014): manufacture a SECOND vnet for the
        # sidecar's eth1, give it a snat=1 subnet (PVE SNATs it out the node's
        # route — PVE-36's confirmed REST-native egress), apply, then fence it.
        egress_vid = _create_egress_vnet(client, zone, backend_name, managed_egress, existing)
        _apply(client)  # one apply stages both vnets + the snat subnet
        _fence_egress_vnet(client, egress_vid, managed_egress.egress_cidr)
        _log.info(
            "created managed-egress SDN vnet %s (switch %s) snat=%s",
            egress_vid,
            backend_name,
            managed_egress.egress_cidr,
        )
        return egress_vid

    _apply(client)
    _log.info("created SDN vnet %s (switch %s) in zone %s", vid, backend_name, zone)
    # For plain uplink+nat, hand back the existing host bridge named by
    # switch.uplink so the orchestrator attaches the sidecar's eth1 to it (the
    # sidecar then MASQUERADEs the isolated vnet out through it). The bridge is
    # static, operator-owned config — not created here, not torn down in
    # destroy_switch. A bare/isolated switch has no uplink segment.
    if switch.uplink is not None and switch.sidecar is not None and switch.sidecar.nat:
        return switch.uplink
    return None


def _create_egress_vnet(
    client: ProxmoxClient,
    zone: str,
    backend_name: str,
    managed_egress: ManagedEgress,
    existing_vnets: set[str],
) -> str:
    """Create the managed-egress vnet + its snat subnet; return the vnet id.

    The subnet carries ``snat=1`` and the ``.1`` gateway, so PVE source-NATs the
    segment out the node's upstream route (no host iptables, no manual bridge —
    this is what ManagedBuildSwitch automates). Staged here; the caller applies.
    """
    egress_vid = egress_vnet_id(backend_name)
    if egress_vid not in existing_vnets:
        client.api.cluster.sdn.vnets.post(
            vnet=egress_vid, zone=zone, alias=f"{backend_name}-egress"
        )
    net = ipaddress.ip_network(managed_egress.egress_cidr, strict=True)
    gateway = str(net.network_address + 1)  # .1 — the backend SNAT gateway
    client.api.cluster.sdn.vnets(egress_vid).subnets.post(
        type="subnet",
        subnet=managed_egress.egress_cidr,
        gateway=gateway,
        snat=1,
    )
    return egress_vid


def _fence_egress_vnet(client: ProxmoxClient, egress_vid: str, egress_cidr: str) -> None:
    """Apply the ADR-0014 §5 default-deny fence on the managed-egress vnet.

    Posture: allow intra-subnet + established/related (PVE's firewall is stateful,
    so return traffic is accepted implicitly) + any destination *not* in RFC1918
    (the internet); drop everything else (host LAN, mgmt, other segments). Built
    from the PVE firewall rule schema — ``type=forward`` rules plus a default
    ``policy_forward=DROP`` backstop — evaluated top-down.

    Confirmed live on PVE 9.2.2 (PVE-37): the SDN VNet-firewall surface
    (``/cluster/sdn/vnets/{vnet}/firewall/{options,rules}``) exists, ``options``
    accepts ``enable`` + ``policy_forward``, and ``rules`` takes ``type=forward``.
    Crucially, PVE **prepends** every posted rule at ``pos`` 0 and *ignores* an
    explicit ``pos`` on this surface — so rules are POSTed in REVERSE of the
    intended evaluation order to land the right top-down precedence.
    """
    fw = client.api.cluster.sdn.vnets(egress_vid).firewall
    fw.options.put(enable=1, policy_forward="DROP")
    # Intended top-down evaluation order. Intra-subnet MUST precede the 10/8 drop
    # because egress_cidr is itself inside 10/8; the catch-all internet ACCEPT
    # MUST come after the RFC1918 drops.
    rules: list[dict[str, str]] = [
        {"action": "ACCEPT", "dest": egress_cidr, "comment": "tr-intra"},
        *({"action": "DROP", "dest": p, "comment": "tr-deny-private"} for p in _RFC1918),
        {"action": "ACCEPT", "comment": "tr-allow-internet"},  # dest omitted = any
    ]
    # PVE prepends at pos 0 (PVE-37), so POST in reverse to land the order above.
    for rule in reversed(rules):
        fw.rules.post(type="forward", enable=1, **rule)


def destroy_switch(client: ProxmoxClient, backend_name: str) -> None:
    vid = vnet_id(backend_name)
    egress_vid = egress_vnet_id(backend_name)
    vnets = client.api.cluster.sdn.vnets.get()
    by_id = {v["vnet"]: v for v in vnets}
    target = by_id.get(vid)
    if target is None:
        return
    zone = target.get("zone")
    client.api.cluster.sdn.vnets(vid).delete()
    removed = {vid}
    # A ManagedBuildSwitch left a second (egress) vnet in the same zone; drop it
    # too. Its snat subnet + firewall config are owned by the vnet and go with it.
    if egress_vid in by_id:
        client.api.cluster.sdn.vnets(egress_vid).delete()
        removed.add(egress_vid)
    # Drop the per-run zone once it holds no more vnets (self-discovering, so a
    # from_uri-rebuilt teardown driver needs no run_id).
    if zone is not None and not any(
        v.get("zone") == zone and v["vnet"] not in removed for v in vnets
    ):
        client.api.cluster.sdn.zones(zone).delete()
        _log.info("destroyed per-run SDN zone %s (last vnet removed)", zone)
    _apply(client)
    _log.info("destroyed SDN vnet(s) %s (switch %s)", sorted(removed), backend_name)


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
