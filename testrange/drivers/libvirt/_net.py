"""L2 fabric for the libvirt backend — via the libvirt network API (BACKEND-1.C).

The driver owns all L2, realized through ``networkDefineXML`` / ``networkCreate``
(NOT pyroute2 — the daemon builds the bridge, so no ``CAP_NET_ADMIN``; ADR-0016).

Each Switch becomes **one** isolated libvirt ``<network>`` (named by the switch's
backend name): no ``<forward>``, no ``<ip>``, no ``<dhcp>`` — pure host-only L2.
The per-Switch sidecar owns DHCP/DNS/NAT; libvirt provides only the bridge. All
Networks on a Switch **share** that one bridge (one wire, multiple labels), which
is what lets a guest on ``pub-a`` reach one on ``pub-b``. STP is off with zero
forward delay so a guest's first DHCP DISCOVER isn't dropped during a learning
window.

For an ``uplink``+``nat`` Switch the sidecar's ``eth1`` needs upstream
connectivity to MASQUERADE out of; on libvirt that is the **pre-existing host
network** ``switch.uplink`` resolves to (e.g. ``tr-egress``, an out-of-band
libvirt NAT network). The driver only *attaches* the sidecar to it — it never
creates, SNATs, fences, or destroys that network (egress is out-of-band). It
returns the resolved name so the orchestrator wires the sidecar's ``eth1`` onto
it; ``create_vm`` then references it as an ordinary ``<interface type='network'>``.

Functions take the live :class:`LibvirtClient`; unit tests inject a duck-typed
fake. Live validation rides the integration suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from testrange._log import get_logger
from testrange.drivers.libvirt import _naming
from testrange.exceptions import DriverError

if TYPE_CHECKING:  # pragma: no cover
    from testrange.drivers.libvirt._conn import LibvirtClient
    from testrange.networks.base import Network, Switch

_log = get_logger(__name__)


def _network_xml(backend_name: str, *, mgmt_host: tuple[str, int] | None) -> str:
    # Isolated guest segment: no <forward> (guests route off-segment only through
    # the sidecar) and no <dhcp> (the sidecar owns DHCP/DNS). stp off + delay 0 so
    # a freshly-attached guest's first DHCP DISCOVER isn't dropped during an STP
    # learning window.
    #
    # ``mgmt_host`` is set only for a Switch with ``mgmt=True``: it puts the host's
    # mgmt adapter (the ``.2`` slot) on the bridge — nothing more (it is not a
    # router or a reachability guarantee; whether an SSHCommunicator can use it is
    # the plan author's concern). <dns enable='no'> stops libvirt spawning a
    # dnsmasq that would shadow the sidecar (with dns off + no dhcp range, libvirt
    # starts no dnsmasq, it just assigns the bridge IP).
    # backend_name is composed by _naming (a safe charset), but escape it as
    # element text anyway — matching the quoteattr/escape discipline the peer
    # _vm.py XML builders use, so the safety doesn't rely on a remote invariant.
    parts = [
        f"<network><name>{escape(backend_name)}</name>",
        # Explicit deterministic bridge name: the nameless form delegates to
        # libvirtd's virbr%d allocator, which races parallel creates (BACKEND-16).
        f"<bridge name={quoteattr(_naming.bridge_name(backend_name))} stp='off' delay='0'/>",
    ]
    if mgmt_host is not None:
        host_ip, prefix = mgmt_host
        parts.append("<dns enable='no'/>")
        parts.append(f"<ip address='{host_ip}' prefix='{prefix}'/>")
    parts.append("</network>")
    return "".join(parts)


def create_switch(
    client: LibvirtClient,
    switch: Switch,
    backend_name: str,
    *,
    resolved_uplink: str | None = None,
) -> str | None:
    """Define + start the Switch's isolated L2 network.

    A ``mgmt=True`` Switch additionally gets the host's ``.2`` mgmt adapter on the
    bridge. Returns the resolved uplink network name (for the sidecar's ``eth1``)
    when the Switch declares an ``uplink`` and a ``nat`` sidecar; ``None``
    otherwise. The uplink network is out-of-band (e.g. ``tr-egress``) — never
    created here.
    """
    mgmt_host = (switch.mgmt_ip, switch.network.prefixlen) if switch.mgmt else None
    net = client.raw.networkDefineXML(_network_xml(backend_name, mgmt_host=mgmt_host))
    net.create()
    _log.info(
        "created libvirt network %s (bridge %s%s)",
        backend_name,
        net.bridgeName(),
        f", mgmt {switch.mgmt_ip}/{switch.network.prefixlen}" if switch.mgmt else "",
    )
    if switch.uplink is not None and switch.sidecar is not None and switch.sidecar.nat:
        if resolved_uplink is None:
            # Preflight catches an unmapped uplink first; fail loud if we get here.
            raise DriverError(
                f"switch {backend_name!r} declares uplink {switch.uplink!r} but the profile "
                "maps no host network for it"
            )
        return resolved_uplink
    return None


def destroy_switch(client: LibvirtClient, backend_name: str) -> None:
    """Stop + undefine the Switch's isolated network. Tolerant of absence.

    Only the per-Switch network (named ``backend_name``) is torn down; the
    out-of-band uplink network is never touched.
    """
    net = client.lookup_network(backend_name)
    if net is None:
        _log.debug("destroy_switch(%s): network not present (already gone)", backend_name)
        return
    if net.isActive():
        net.destroy()
    if net.isPersistent():
        net.undefine()
    _log.info("destroyed libvirt network %s", backend_name)


def create_network(
    client: LibvirtClient,
    network: Network,
    switch: Switch,
    backend_name: str,
    *,
    switch_backend_name: str,
) -> str:
    """Attach a Network to its Switch's shared bridge.

    Networks share the Switch's single libvirt network (created in
    ``create_switch``), so this records no new backend object — it resolves to
    that network's name. The driver remembers ``backend_name → switch network``
    so ``create_vm`` can wire a NIC declared against this Network onto the right
    libvirt network.
    """
    del client, network, switch, backend_name
    return switch_backend_name


def destroy_network(client: LibvirtClient, backend_name: str) -> None:
    """No-op: Networks share their Switch's network, torn down by destroy_switch."""
    del client, backend_name
