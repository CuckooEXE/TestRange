Proxy / port-forward
====================

When a TestRange orchestrator runs against a remote bare-metal
hypervisor, the inner-VM network is routable from the **hypervisor**
but not from the **test runner**.  Tests that need raw TCP to an
inner-VM IP — a custom protocol probe, a third-party SDK that
doesn't speak guest-agent, a ``curl`` from the runner instead of
from a sidecar VM — used to be stuck:

* Running on the host: an ``ip route add`` punches through, but
  pollutes routing globally and needs ``CAP_NET_ADMIN``.
* Running from a sidecar VM on the same network: works, but adds a
  permanent VM to every test topology that needs runner-side access.

:meth:`~testrange.orchestrator_base.AbstractOrchestrator.proxy`
returns a backend-supplied tunnel that solves this without any
host-level routing changes:

.. code-block:: python

   pj = orch.proxy()                       # memoized per orchestrator
   sock = pj.connect(("10.50.0.2", 80))     # raw socket
   bind = pj.forward(("10.50.0.2", 80))     # local listener address

The proxy is registered with the orchestrator's ExitStack — no
explicit teardown needed in tests.

Two methods, two integration shapes
-----------------------------------

``connect((host, port)) -> socket.socket``
    Opens a single TCP tunnel to the inner endpoint and returns a
    connected socket-shaped handle.  Use this when the client you're
    integrating accepts a ``sock=`` parameter:

    * :meth:`paramiko.SSHClient.connect` (``sock=``)
    * :class:`requests.adapters.HTTPSAdapter` with a custom
      ``init_poolmanager``
    * :mod:`asyncio` low-level ``loop.create_connection(sock=)``

    .. code-block:: python

       import paramiko
       sock = orch.proxy().connect(("10.50.0.2", 22))
       inner = paramiko.SSHClient()
       inner.connect("10.50.0.2", sock=sock, username="root",
                     key_filename="/path/to/key")

``forward((host, port), bind=("127.0.0.1", 0)) -> (host, port)``
    Opens a local listener on the test runner that pipes through a
    fresh tunnel channel per accepted connection.  Returns the
    actual ``(host, port)`` the listener bound to (with ``bind``'s
    default port=0, the OS picks an ephemeral port).  Use this when
    the client only knows how to connect to a ``host:port`` string:

    * ``curl`` shelled out of a test
    * :class:`proxmoxer.ProxmoxAPI` against an unreachable PVE
    * Any third-party SDK with hard-coded URL building

    .. code-block:: python

       bind_host, bind_port = orch.proxy().forward(("10.50.0.2", 80))
       url = f"http://{bind_host}:{bind_port}/"
       subprocess.run(["curl", "-fsS", url], check=True)

Backend support matrix
----------------------

.. list-table::
   :header-rows: 1

   * - Backend
     - Status
     - Tunnel mechanism
   * - ``LibvirtOrchestrator`` (``qemu+ssh://``)
     - ✅ shipped
     - paramiko ``Transport.open_channel("direct-tcpip", ...)`` over
       the same SSH coords parsed from the libvirt URI
   * - ``LibvirtOrchestrator`` (``qemu:///system``, local)
     - ⚠️ raises
     - No remote SSH to tunnel through; the runner is already on
       the libvirt bridge.  Reach inner VMs by IP directly.
   * - ``ProxmoxOrchestrator`` (built via ``root_on_vm``)
     - ✅ shipped
     - paramiko ``direct-tcpip`` over the answer.toml-baked SSH
       key from the hypervisor's communicator.  No explicit creds
       needed in test code.
   * - ``ProxmoxOrchestrator`` (operator-supplied PVE cluster)
     - ✅ shipped (with explicit creds)
     - Pass ``ssh_user=`` plus ``ssh_key_filename=`` (or
       ``ssh_password=``) to the constructor.  ``proxy()`` raises
       a clear error if either is missing.
   * - ESXi
     - 🚧 not yet
     - Planned: paramiko ``direct-tcpip`` for ESXi 7+ (sshd
       supports TCP forwarding); ProxyCommand-via-``nc`` for
       pre-7 hosts.
   * - Hyper-V
     - 🚧 not yet
     - Planned: paramiko ``direct-tcpip`` over Windows OpenSSH
       Server (``Add-WindowsCapability OpenSSH.Server``); WinRM
       ``netsh interface portproxy`` for hosts without OpenSSH.

Common patterns
---------------

Reaching a PVE API on a remote-libvirt-hosted PVE
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When PVE is on a libvirt bridge whose IP the test runner can't
route to, but the libvirt host is reachable over SSH, the outer
orchestrator's ``proxy().forward()`` bridges the gap:

.. code-block:: python

   from proxmoxer import ProxmoxAPI

   bind_host, bind_port = libvirt_orch.proxy().forward(
       (pve_internal_ip, 8006)
   )
   api = ProxmoxAPI(
       host=f"{bind_host}:{bind_port}",
       user="root@pam", password=password,
       verify_ssl=False,
   )

Reaching an inner SDN vnet from the test runner
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inner VMs on a PVE SDN vnet have IPs (e.g. ``10.50.0.x``) that
aren't on any libvirt bridge and aren't routable from the test
runner.  ``inner.proxy().forward(...)`` tunnels through the PVE
node:

.. code-block:: python

   inner_orch = orch._inner_orchestrators[0]   # ProxmoxOrchestrator
   bind = inner_orch.proxy().forward(("10.50.0.2", 80))
   # urllib / requests / curl can all hit f"http://{bind[0]}:{bind[1]}/"

This is what
:func:`examples.nested_proxmox_airgapped._verify_runner_reaches_inner_via_proxy`
demonstrates as the runner-side counterpart to the inner-VM-to-VM
``curl http://10.50.0.2/`` assertion above it.

Lifecycle
---------

* The proxy is **memoized** per orchestrator — repeated
  ``orch.proxy()`` calls return the same instance.  One SSH
  transport, many channels.
* The proxy is **torn down** when the orchestrator's ``__exit__``
  fires.  Test authors don't need to call ``proxy.close()``
  manually — but it's safe to do so for tighter teardown ordering.
* ``forward`` listeners run in **daemon threads**.  A test process
  that exits without explicit teardown won't hang on these.
* Tunnels share the underlying SSH transport, so opening 50 forwards
  pays one TCP+SSH handshake total.

Troubleshooting
---------------

``OrchestratorError: ... is for remote libvirt hosts``
    You called ``orch.proxy()`` on a local libvirt orchestrator
    (``host="localhost"`` or ``qemu:///system``).  The runner is
    already on the libvirt bridge — reach inner VMs by IP directly
    instead of going through a tunnel.

``OrchestratorError: ... needs SSH credentials``
    You called ``ProxmoxOrchestrator.proxy()`` on an
    operator-supplied PVE cluster without providing ``ssh_user`` +
    ``ssh_key_filename`` (or ``ssh_password``).  Pass them at
    construction time.  When the orchestrator is built via
    ``root_on_vm``, these are populated automatically from the
    hypervisor's communicator — if you're seeing this error after
    ``root_on_vm``, the hypervisor's ``users=`` doesn't include a
    credential with ``ssh_key`` set.

``OrchestratorError: open_channel(direct-tcpip, ...) failed``
    The SSH server refused the channel.  Most common causes:

    * SSH server has ``AllowTcpForwarding no`` (rare on libvirt /
      PVE; common on locked-down ESXi hosts).
    * The target is unreachable from the hypervisor's network
      namespace (wrong IP, network not up).
    * The destination port has no listener (the inner VM isn't
      running the service yet).

When the hypervisor can't see the VM
------------------------------------

The proxy reaches whatever the **hypervisor itself** can reach at
L3.  That's a deliberate constraint — there's no way for the proxy
to route to an inner VM that the hypervisor can't route to either.
A few topologies cut the hypervisor out of the L3 path:

* **Bridges with no host IP** — a libvirt network defined with
  :class:`testrange.VirtualNetwork(host_isolated=True)
  <testrange.backends.libvirt.network.VirtualNetwork>`, or any
  manual Linux bridge the operator created without giving the host
  an IP on it.  L2 between VMs works; L3 from the host doesn't.
* **PCIe / SR-IOV passthrough** — VM has a physical NIC attached
  directly, hypervisor doesn't bridge the traffic at all.
* **Macvtap private mode** — host and VM share a physical NIC but
  the kernel deliberately segregates them.
* **VLAN-tagged interfaces the host doesn't terminate** — Proxmox
  SDN VLAN zones where PVE has the trunk but no L3 sub-interface
  on the specific VLAN.
* **Cross-vSwitch isolation on ESXi / Hyper-V** — VM on a vSwitch
  whose uplink doesn't carry the host's management vmk port.

In these topologies, ``proxy.connect()`` / ``proxy.forward()`` will
fail at the SSH ``open_channel`` step (hypervisor's sshd reports
"network is unreachable" or "channel administratively prohibited").
The error message names the target and points at hypervisor
reachability.

Workaround: run the assertion **from a sidecar VM** that *is* on
the same network as the target.  The orchestrator already exposes
that VM via ``orch.vms[<name>]`` and its ``communicator`` rides
virtio-serial (host-mediated, not IP-routed), so the test runner
can ``vm.exec(["curl", ...])`` even on an isolated bridge.

See :file:`examples/host_isolated_network.py` for a worked
demonstration of the mutual-isolation pattern + the sidecar
workaround.
