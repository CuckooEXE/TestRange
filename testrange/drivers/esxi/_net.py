"""L2 fabric for the ESXi backend — standard vSwitch + portgroup (ESXI-2).

Standalone host only (ADR-0025): standard vSwitch + portgroup; DVS/dvportgroup
are vCenter-only and out of scope.

Per-Switch model
----------------
Each Switch is realized as one **isolated standard vSwitch** (no physical
uplink) named deterministically from the Switch backend name. Each Network on
the Switch becomes a **portgroup** on that vSwitch, all VLAN 0 — so multiple
Networks share one L2 broadcast domain (one wire, multiple labels), matching the
portable plan's multi-Network-per-Switch reach. A VM NIC binds to a portgroup by
name; ``create_vm`` resolves ``network_refs`` to portgroup names.

mgmt (``Switch(mgmt=True)``)
----------------------------
A mgmt Switch additionally gets the host's ``.2`` adapter as a **VMkernel NIC**
on a dedicated portgroup of the isolated vSwitch (ADR-0009 B analog): a plain
host L2 presence at ``mgmt_ip`` coexisting with the sidecar at ``.1``. Static IP,
no SNAT.

uplink + NAT
------------
For an ``uplink``+``nat`` Switch the sidecar's ``eth1`` needs a segment with
upstream connectivity to MASQUERADE out of. On ESXi that is a portgroup on a
**shared uplink vSwitch** that enslaves the resolved physical NIC (``switch.uplink``
is a logical name, ADR-0016; the profile's ``[uplinks]`` map resolves it to a
pNIC like ``vmnic1``). A pNIC belongs to exactly one vSwitch, so every NAT Switch
resolving to the same uplink shares that one uplink vSwitch and gets its own
uplink portgroup; ``create_switch`` returns that portgroup name. The pNIC and its
upstream are operator-owned/out-of-band — the driver enslaves the pNIC onto a
vSwitch it owns but never SNATs or fences it.

Teardown is self-discovering (a ``from_uri`` driver with no in-process map):
names are pure functions of the backend name, the uplink vSwitch is read off the
uplink portgroup's ``vswitchName`` before deletion and GC'd when its last
``tr-`` portgroup goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange._log import get_logger
from testrange.drivers.esxi import _naming
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.esxi._client import EsxiClient
    from testrange.networks.base import Network, Switch

_log = get_logger(__name__)

_DEFAULT_PORTS = 128


def _vswitch_names(client: EsxiClient) -> set[str]:
    return {vs.name for vs in client.host.config.network.vswitch}


def _portgroup_names(client: EsxiClient) -> set[str]:
    return {pg.spec.name for pg in client.host.config.network.portgroup}


def _add_vswitch(client: EsxiClient, name: str, *, pnic: str | None = None) -> None:
    """Idempotently add a standard vSwitch, optionally enslaving a physical NIC."""
    if name in _vswitch_names(client):
        return
    vim = client.vim
    spec = vim.host.VirtualSwitch.Specification(numPorts=_DEFAULT_PORTS)
    if pnic is not None:
        spec.bridge = vim.host.VirtualSwitch.BondBridge(nicDevice=[pnic])
    client.network_system.AddVirtualSwitch(vswitchName=name, spec=spec)
    _log.info("created vSwitch %s%s", name, f" (uplink {pnic})" if pnic else " (isolated)")


def _add_portgroup(client: EsxiClient, name: str, vswitch: str) -> None:
    """Idempotently add a VLAN-0 portgroup on ``vswitch``."""
    if name in _portgroup_names(client):
        return
    vim = client.vim
    spec = vim.host.PortGroup.Specification(
        name=name, vlanId=0, vswitchName=vswitch, policy=vim.host.NetworkPolicy()
    )
    client.network_system.AddPortGroup(portgrp=spec)
    _log.info("created portgroup %s on vSwitch %s", name, vswitch)


def _add_mgmt_vmk(client: EsxiClient, portgroup: str, switch: Switch) -> None:
    """Add the host's ``.2`` mgmt VMkernel NIC on ``portgroup`` (idempotent)."""
    # A vnic already on this portgroup (a re-entrant create_switch) is left alone.
    for vnic in client.host.config.network.vnic:
        if vnic.portgroup == portgroup:
            return
    vim = client.vim
    ip = vim.host.IpConfig(
        dhcp=False, ipAddress=switch.mgmt_ip, subnetMask=str(switch.network.netmask)
    )
    spec = vim.host.VirtualNic.Specification(ip=ip)
    device = client.network_system.AddVirtualNic(portgroup=portgroup, nic=spec)
    _log.info("added mgmt vmk %s (%s) on portgroup %s", device, switch.mgmt_ip, portgroup)


def create_switch(
    client: EsxiClient,
    switch: Switch,
    backend_name: str,
    *,
    resolved_uplink: str | None = None,
) -> str | None:
    """Realize a Switch's L2: an isolated vSwitch + (mgmt vmk) + (uplink segment).

    Returns the uplink portgroup name for an ``uplink``+``nat`` Switch (the
    sidecar's ``eth1`` segment), else ``None``.
    """
    vswitch = _naming.vswitch_name(backend_name)
    _add_vswitch(client, vswitch)

    if switch.mgmt:
        mgmt_pg = _naming.mgmt_portgroup_name(backend_name)
        _add_portgroup(client, mgmt_pg, vswitch)
        _add_mgmt_vmk(client, mgmt_pg, switch)

    if switch.uplink is not None and switch.sidecar is not None and switch.sidecar.nat:
        if resolved_uplink is None:  # pragma: no cover - guarded by the driver
            raise DriverError(
                f"switch {switch.name!r} is uplink+nat but no physical NIC was resolved"
            )
        up_vswitch = _naming.uplink_vswitch_name(resolved_uplink)
        _add_vswitch(client, up_vswitch, pnic=resolved_uplink)
        up_pg = _naming.uplink_portgroup_name(backend_name)
        _add_portgroup(client, up_pg, up_vswitch)
        return up_pg
    return None


def create_network(
    client: EsxiClient,
    network: Network,
    switch: Switch,
    backend_name: str,
    *,
    switch_backend_name: str,
) -> str:
    """Attach a Network as a portgroup on its Switch's isolated vSwitch.

    Returns the portgroup name the orchestrator threads as the network backend;
    ``create_vm`` binds a NIC to it by name.
    """
    del network, switch
    vswitch = _naming.vswitch_name(switch_backend_name)
    pg = _naming.portgroup_name(backend_name)
    _add_portgroup(client, pg, vswitch)
    return pg


def destroy_network(client: EsxiClient, backend_name: str) -> None:
    """Remove a Network's portgroup. Tolerant of absence (idempotent teardown)."""
    pg = _naming.portgroup_name(backend_name)
    if pg not in _portgroup_names(client):
        return
    client.network_system.RemovePortGroup(pgName=pg)
    _log.info("removed portgroup %s", pg)


def _remove_vmk_on(client: EsxiClient, portgroup: str) -> None:
    for vnic in client.host.config.network.vnic:
        if vnic.portgroup == portgroup:
            client.network_system.RemoveVirtualNic(vnic.device)
            _log.info("removed mgmt vmk %s on portgroup %s", vnic.device, portgroup)


def destroy_switch(client: EsxiClient, backend_name: str) -> None:
    """Tear down a Switch's whole L2 fabric. Self-discovering and idempotent.

    Removes (in dependency order): the mgmt vmk + portgroup, the uplink portgroup
    (and the shared uplink vSwitch once its last ``tr-`` portgroup goes), any
    remaining guest portgroups on the isolated vSwitch, then the isolated vSwitch
    itself. A ``from_uri`` driver recomputes every name from ``backend_name`` and
    reads the uplink vSwitch off the portgroup, so no in-process state is needed.
    """
    vswitch = _naming.vswitch_name(backend_name)
    mgmt_pg = _naming.mgmt_portgroup_name(backend_name)
    up_pg = _naming.uplink_portgroup_name(backend_name)

    pgs = {pg.spec.name: pg for pg in client.host.config.network.portgroup}

    # mgmt vmk first (a portgroup with a vnic can't be removed), then its pg.
    if mgmt_pg in pgs:
        _remove_vmk_on(client, mgmt_pg)
        client.network_system.RemovePortGroup(pgName=mgmt_pg)

    # uplink portgroup on the shared uplink vSwitch; GC that vSwitch when empty.
    if up_pg in pgs:
        up_vswitch = pgs[up_pg].spec.vswitchName
        client.network_system.RemovePortGroup(pgName=up_pg)
        _log.info("removed uplink portgroup %s", up_pg)
        remaining = [
            pg
            for pg in client.host.config.network.portgroup
            if pg.spec.vswitchName == up_vswitch and pg.spec.name.startswith("tr")
        ]
        if not remaining and up_vswitch in _vswitch_names(client):
            client.network_system.RemoveVirtualSwitch(up_vswitch)
            _log.info("removed shared uplink vSwitch %s (last portgroup gone)", up_vswitch)

    # any remaining guest portgroups on the isolated vSwitch, then the vSwitch.
    for pg in client.host.config.network.portgroup:
        if pg.spec.vswitchName == vswitch:
            client.network_system.RemovePortGroup(pgName=pg.spec.name)
    if vswitch in _vswitch_names(client):
        client.network_system.RemoveVirtualSwitch(vswitch)
        _log.info("removed vSwitch %s", vswitch)
