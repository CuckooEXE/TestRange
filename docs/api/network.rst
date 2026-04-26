Networks
========

A TestRange :class:`~testrange.AbstractVirtualNetwork` is a logical
L2 segment with optional DHCP, DNS, and NAT forwarding.  How that
materialises depends on the backend — see :doc:`backends` for the
per-backend mapping (host-managed bridges, SDN vnets, …).  The
three boolean flags below describe the topology in backend-neutral
terms.

``internet``
    If ``True``, the backend installs forwarding rules so guests on
    this network can reach the host's default route.  If ``False``,
    the network is "isolated" — no forwarding rules are installed,
    and guests can only talk to one another on the bridge.  This
    flag also controls whether the network contributes a
    ``gateway4`` entry to each guest's cloud-init network-config
    (isolated networks never should, or you end up with multiple
    default routes fighting for egress).

``dhcp``
    Whether the backend's bridge-local DHCP service hands out leases.
    Turning this off forces every guest to be given a static IP via
    :class:`~testrange.devices.vNIC`; the orchestrator
    will raise if a network has ``dhcp=False`` and any attached NIC
    lacks an explicit ``ip=``.

``dns``
    Whether the backend's bridge-local DNS service answers
    ``<vmname>.<netname>`` queries for every VM registered on the
    network.  The network name acts as a pseudo-TLD so cross-network
    lookups are explicit about which network a name belongs to
    (``db.Private`` vs ``db.Public`` are always distinct addresses,
    never ambiguous).  The bare ``<vmname>`` is *not* registered —
    it's the VM's own hostname, resolvable via ``/etc/hosts`` inside
    the VM itself but not across the bridge.  Turning this flag off
    disables the bridge-local DNS service entirely (DHCP only) —
    useful when the host already has its own DNS resolver bound
    wildcard on port 53.

The three are orthogonal: a network can have DHCP without DNS, DNS
without internet, or all three at once.  The library's own install
network always sets ``dhcp=True, internet=True, dns=True`` so cloud
images can resolve their package mirrors.

Run-scoped naming
-----------------

Backends typically cap network and bridge names at 15 characters.
Each network gets a truncated, run-ID-suffixed identifier
(e.g. ``tr-intern-a1b2``) and a matching bridge (e.g.
``vbrintera1b2``) so concurrent or consecutive runs never collide.
The logical ``name`` you pass in is preserved for hostname / ref
matching and is used for fully-qualified DNS lookups.

Design notes
------------

**MAC addresses are deterministic.**  Each VM's MAC on a given
network is derived from ``sha256("<vm>:<netname>")`` with the
``52:54:00`` OUI prefix.  This keeps the cloud-init network-config
stable across runs and lets the install-phase DHCP reservation line
up with the run-phase reservation without any state being shared.

**Static IPs are DHCP reservations, not manual config.**  Passing
``vNIC("NetA", ip="10.0.0.5")`` registers that MAC/IP pair with
the backend's bridge-local DHCP service and writes it into the
VM's cloud-init network-config.  The guest sees a static IP; the
backend still serves it as a DHCP lease.

Reference
---------

.. autoclass:: testrange.networks.base.AbstractVirtualNetwork
   :members:
   :show-inheritance:

Concrete backend-specific ``<Backend>VirtualNetwork`` classes are
documented under :doc:`backends`.
