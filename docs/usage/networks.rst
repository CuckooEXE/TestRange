Networks
========

TestRange networks describe an isolated network segment for a test
run.  Three boolean flags shape the topology; each can be toggled
independently.  The concrete realisation depends on the hypervisor
backend — see :doc:`/api/backends` for the per-backend mapping
(host-managed bridges with bridge-local DHCP/DNS, SDN vnets, …).

Two layers
----------

The networking surface has two layers, mirroring the standard
L2-virtualisation model used in ESXi (vSwitch + Port Group),
Proxmox SDN (Zone + VNet), and similar:

* :class:`~testrange.Switch` — an L2 switch (or its backend
  equivalent).  Hosts one or more virtual networks; can carry
  physical-NIC uplinks; has a backend-specific *type* knob for
  backends that ship multiple switch flavours (e.g. PVE SDN's
  ``simple`` / ``vlan`` / ``vxlan`` / ``evpn`` zones).
* :class:`~testrange.VirtualNetwork` — the named thing VMs attach
  to (your "port group").  Optionally bound to a switch via
  ``switch=`` on the constructor.

For most tests the switch layer is invisible — every backend
provides a sensible default (libvirt: each network is its own
self-contained bridge; Proxmox: a shared ``simple`` SDN zone) and
``VirtualNetwork(name, subnet, ...)`` works exactly as before.
Reach for an explicit :class:`Switch` only when you need to:

* run multiple networks under one zone with shared uplinks
  (Proxmox VLAN zones, ESXi vSwitches with several port groups);
* bind a network to a specific physical NIC for upstream egress;
* select a non-default zone type (VXLAN, VLAN trunking, EVPN BGP).

See :ref:`switch-explicit-example` below for the explicit form.

Topology flags
--------------

.. list-table::
   :header-rows: 1
   :widths: 10 45 45

   * - Flag
     - ``True``
     - ``False``
   * - ``internet``
     - NAT forwarding to the host's default route.  Guests can reach
       the public internet.  The backend installs the forwarding
       rules when the network starts and tears them down on stop.
     - No forwarding rules.  Guests on this bridge can only talk to
       each other.  Useful for asserting that a service works (or
       correctly fails) without internet access.
   * - ``dhcp``
     - The backend's bridge-local DHCP service hands out leases.
       NICs without an explicit ``ip=`` get a deterministic address
       via MAC reservation.
     - No DHCP.  Every ``vNIC`` attached to this network
       must pass ``ip="..."``; the orchestrator raises otherwise.
   * - ``dns``
     - The backend's bridge-local DNS service resolves
       ``<vmname>.<netname>`` for every VM registered on the
       network.  The network name acts as a pseudo-TLD — from the
       ``client`` VM, ``curl http://webpublic.Internet/`` reaches the
       VM named ``webpublic`` on the network named ``Internet``.
       Bare ``<vmname>`` is intentionally *not* registered; it
       forces every cross-VM lookup to spell out which network the
       name lives on.
     - DNS is disabled — DHCP only.  Useful when the host has its
       own DNS service bound wildcard (see
       :doc:`/usage/installation` for the coexistence note).

Typical recipes
---------------

**Single flat network with internet.**

.. code-block:: python

    VirtualNetwork("Public", "10.0.0.0/24", internet=True, dhcp=True, dns=True)

**Two-tier: public internet + isolated private.**

.. code-block:: python

    networks=[
        VirtualNetwork("Public",  "10.0.1.0/24", internet=True,  dhcp=True,  dns=True),
        VirtualNetwork("Private", "10.0.2.0/24", internet=False, dhcp=False, dns=True),
    ]

The dual-homed VM sits on both; the private-only VM sits on the
second.  With ``dhcp=False`` on the private network, every NIC there
needs an explicit ``ip=`` — this is exactly what
``examples/two_networks_three_vms.py`` does.

**Air-gapped lab.**

.. code-block:: python

    VirtualNetwork("Airgap", "10.99.0.0/24", internet=False, dhcp=False, dns=False)

No DHCP, no DNS, no NAT.  All communication between VMs is by IP
only.  Use this to test configuration that ships static hosts files
or hard-coded service endpoints.

Static IPs
----------

Passing ``vNIC("NetA", ip="10.0.0.5")`` registers that
MAC/IP pair with the backend's DHCP/DNS service (when DHCP is on)
and writes it into the guest's cloud-init network-config.  Two
consequences:

1. The guest sees a stable address across reboots and across
   TestRange runs (MAC is derived from ``sha256("<vm>:<net>")``).

2. The DNS entry that pairs with the reservation makes
   ``curl http://server.NetA/`` work even on a DHCP-less network
   (note: always FQDN — bare ``server`` does not resolve across the
   bridge).

Static IPs *without* ``internet=True`` also omit ``gateway4`` from
the cloud-init network-config, so an isolated NIC cannot accidentally
become the VM's default route.

DHCP-discovery vNICs
~~~~~~~~~~~~~~~~~~~~

Omitting ``ip=`` on a vNIC asks the orchestrator to pick an address
for you:

.. code-block:: python

    vNIC("NetA")  # no ip= → orchestrator allocates one

The orchestrator walks the network's subnet host range in declaration
order, skips the gateway (``.1``) and any address an earlier vNIC
already claimed (static or auto-allocated), and registers the first
free host with ``register_vm``.  The picked address is threaded into
cloud-init / answer.toml exactly as if you had written it as
``ip=`` yourself — the guest still gets a stable, deterministic
address.

* **Determinism:** the Nth DHCP-discovery vNIC in declaration order
  always lands on the Nth host address.  Test assertions that name
  expected IPs stay stable across re-runs.
* **Subnet-exhausted:** raises :class:`NetworkError` naming the VM
  and subnet rather than silently spinning.  Either add explicit
  ``ip=`` values or widen the subnet.

Both backends accept the no-``ip=`` form: libvirt threads it through
its bridge-local DHCP reservation, Proxmox writes the picked address
into the VM's cloud-init seed (PVE SDN doesn't run a per-vnet DHCP
service, so the deterministic-pick approach gives the same stable-IP
guarantee without one).

.. _proxmox-networking-knobs:

Proxmox: install-vnet pool and ``install_dns``
----------------------------------------------

Two operator-facing knobs on
:class:`~testrange.backends.proxmox.ProxmoxOrchestrator` shape what
guests see during the install pass and how concurrent runs share
one PVE cluster.

**Install-vnet subnet pool.**  Every Proxmox run creates a per-run
SDN vnet (``inst<run_id[:4]>``) so cloud-init / answer.toml can
reach upstream package mirrors regardless of whether any
user-declared network has internet.  At ``__enter__`` the
orchestrator picks one subnet from a 10-entry pool spanning
``192.168.230.0/24`` – ``192.168.239.0/24`` (sits below libvirt's
``240.0/24``+ pool to avoid cross-backend collision when both run on
the same host).  PVE's ``cluster/sdn/subnets`` is queried once and
the first pool entry not already claimed by another in-flight run on
the cluster wins.

* Capacity is 10 concurrent runs against one PVE cluster.  The
  picker raises :class:`OrchestratorError` with hints when every
  pool entry is in use rather than silently colliding — at that
  scale the operator wants the backpressure.
* Larger CI fleets can widen the pool by editing
  ``_INSTALL_SUBNET_POOL`` in
  ``testrange/backends/proxmox/orchestrator.py`` (one-line change;
  the rest of the picker iterates the tuple).
* Crashed runs that leave ``inst<run_id[:4]>`` vnets behind are
  swept by the next ``testrange cleanup MODULE RUN_ID`` against the
  same orchestrator factory.

**``install_dns=`` resolver pin.**  PVE SDN simple-zones don't ship a
per-bridge DNS resolver (libvirt's dnsmasq pattern doesn't apply), so
the orchestrator pins one resolver address that cloud-init /
answer.toml advertise to every guest:

.. code-block:: python

    ProxmoxOrchestrator(
        host="pve.example.com",
        user="root@pam",
        token_name="testrange",
        token_value="...",
        install_dns="10.0.0.53",   # internal resolver, default "1.1.1.1"
        networks=[...],
        vms=[...],
    )

Override for air-gapped, sovereign-DNS, or split-horizon setups
where ``1.1.1.1`` either isn't reachable or is the wrong answer.
The same value also resolves run-phase NICs on networks declared
``dns=True`` — without the pin, a ``dns=True`` Proxmox network would
otherwise leave the guest's ``/etc/resolv.conf`` pointing at the SDN
gateway IP, which is just a router.  ``dns=False`` networks continue
to leave the guest's ``nameserver`` empty.

Coexisting with a host-level DNS service
----------------------------------------

If the backend brings its own bridge-local DNS service and the
host already runs a wildcard-bound DNS server on
``0.0.0.0:53``, the two can collide with ``EADDRINUSE`` when a
test starts.  The fix is host-level — the exact configuration
depends on which DNS server you run and which backend you use;
each backend's docstring under :mod:`testrange.backends` lists
the bridge-interface name patterns to except.

.. _switch-explicit-example:

Multiple networks per switch (explicit ``Switch``)
--------------------------------------------------

When you want several networks to share one switch — typically
because they need to share uplinks or live in a non-default zone
type — declare the switch explicitly and pass it to both the
orchestrator (``switches=``) and each network (``switch=``):

.. code-block:: python

    from testrange import (
        Switch, VirtualNetwork, Orchestrator, Test, VM,
        Credential, Memory, vCPU, vNIC, run_tests,
    )
    from testrange.backends.proxmox import ProxmoxOrchestrator

    corp = Switch(
        "Corp",
        switch_type="vlan",   # PVE SDN zone type
        uplinks=["eno1"],     # bind to this physical NIC
    )

    mgmt = VirtualNetwork(
        "Mgmt", "10.0.10.0/24",
        internet=True, dhcp=True,
        switch=corp,          # ← lives in the Corp zone
    )
    prod = VirtualNetwork(
        "Prod", "10.0.20.0/24",
        internet=True, dhcp=True,
        switch=corp,          # ← same zone, different vnet
    )

    tests = [
        Test(
            ProxmoxOrchestrator(
                host="pve.example.com",
                user="root@pam",
                password="...",
                switches=[corp],
                networks=[mgmt, prod],
                vms=[
                    VM(
                        name="web", iso="...",
                        users=[Credential("root", "pw")],
                        devices=[vCPU(2), Memory(2), vNIC("Mgmt")],
                    ),
                ],
            ),
            lambda orch: None,
            name="vlan-trunk-smoke",
        ),
    ]

What each backend does with the switch:

* **Proxmox** — creates a PVE SDN zone of the requested type
  (``simple`` / ``vlan`` / ``qinq`` / ``vxlan`` / ``evpn``).
  ``uplinks=`` flows into the zone's ``bridge=`` for VLAN/QinQ
  zones; ``zone_extra={...}`` is a free-form ``dict`` for VXLAN
  / EVPN knobs TestRange doesn't model first-class
  (``peers``, ``vrf-vxlan``, ``controller``).
* **Libvirt** — accepts the field for portability but ignores
  it; libvirt's network model puts every network on its own
  bridge with no separate switch layer.

Backwards compatibility: every ``VirtualNetwork(...)`` without
a ``switch=`` keeps working exactly as before — Proxmox lands
it in the orchestrator's default ``"tr"`` simple zone; libvirt
treats it as a self-contained NAT/isolated bridge.

See ``examples/proxmox_explicit_zones.py`` for a runnable
end-to-end version with multiple zone types.
