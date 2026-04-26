Networks
========

TestRange networks describe an isolated network segment for a test
run.  Three boolean flags shape the topology; each can be toggled
independently.  The concrete realisation depends on the hypervisor
backend — see :doc:`/api/backends` for the per-backend mapping
(host-managed bridges with bridge-local DHCP/DNS, SDN vnets, …).

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

Coexisting with a host-level DNS service
----------------------------------------

If the backend brings its own bridge-local DNS service and the
host already runs a wildcard-bound DNS server on
``0.0.0.0:53``, the two can collide with ``EADDRINUSE`` when a
test starts.  The fix is host-level — the exact configuration
depends on which DNS server you run and which backend you use;
each backend's docstring under :mod:`testrange.backends` lists
the bridge-interface name patterns to except.
