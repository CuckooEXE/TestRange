Networks
========

TestRange networks describe an isolated network segment for a test
run.  Three boolean flags shape the topology; each can be toggled
independently.  The concrete realisation depends on the hypervisor
backend — under the default libvirt backend each network becomes a
libvirt-managed bridge with dnsmasq-served DHCP and DNS; under
Proxmox it would map to an SDN vnet (see :doc:`/api/backends`).

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
       the public internet.  Libvirt installs MASQUERADE iptables
       rules when the network starts and tears them down on stop.
     - No forwarding rules.  Guests on this bridge can only talk to
       each other.  Useful for asserting that a service works (or
       correctly fails) without internet access.
   * - ``dhcp``
     - dnsmasq hands out DHCP leases.  NICs without an explicit
       ``ip=`` get a deterministic address via MAC reservation.
     - No DHCP.  Every ``VirtualNetworkRef`` attached to this network
       must pass ``ip="..."``; the orchestrator raises otherwise.
   * - ``dns``
     - dnsmasq listens on port 53 on the bridge IP and resolves
       ``<vmname>.<netname>`` for every VM registered on the
       network.  The network name acts as a pseudo-TLD — from the
       ``client`` VM, ``curl http://webpublic.Internet/`` reaches the
       VM named ``webpublic`` on the network named ``Internet``.
       Bare ``<vmname>`` is intentionally *not* registered; it
       forces every cross-VM lookup to spell out which network the
       name lives on.
     - dnsmasq runs with ``--port=0`` — DHCP only, no DNS socket.
       Useful when the host has its own dnsmasq bound wildcard (see
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

Passing ``VirtualNetworkRef("NetA", ip="10.0.0.5")`` registers that
MAC/IP pair as a dnsmasq reservation (if DHCP is on) and writes it
into the guest's cloud-init network-config.  Two consequences:

1. The guest sees a stable address across reboots and across
   TestRange runs (MAC is derived from ``sha256("<vm>:<net>")``).

2. The dnsmasq ``<host>`` entry also feeds DNS, so
   ``curl http://server.NetA/`` works even on a DHCP-less network
   (note: always FQDN — bare ``server`` does not resolve across the
   bridge).

Static IPs *without* ``internet=True`` also omit ``gateway4`` from
the cloud-init network-config, so an isolated NIC cannot accidentally
become the VM's default route.

Coexisting with a host-level dnsmasq
------------------------------------

If the host is already running its own ``dnsmasq`` (bound wildcard
on ``0.0.0.0:53``), libvirt's per-bridge dnsmasq will fail to bind
``<bridge-ip>:53`` with ``EADDRINUSE``.  The fix is host-level: make
the system dnsmasq bind dynamically to specific interfaces and
except libvirt's bridges:

.. code-block:: ini

    # /etc/dnsmasq.d/90-libvirt-compat.conf
    bind-dynamic
    except-interface=virbr*
    except-interface=vbr*
    except-interface=vnet*

Then ``sudo systemctl restart dnsmasq``.  Now the system resolver
keeps answering queries on your real interfaces, but the libvirt
bridges are left alone for their own dnsmasq instances.
