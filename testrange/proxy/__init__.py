"""Proxy / port-forwarder abstractions for tunnelling test-runner
traffic into a hypervisor's inner-VM network namespace.

The construct solves the "remote bare-metal hypervisor + unreachable
inner IP" problem without requiring ``ip route add`` on the test
runner: every backend exposes ``orch.proxy()`` which returns a
:class:`Proxy` instance whose lifetime is tied to the orchestrator's
:class:`~contextlib.ExitStack`.  Test authors then either:

* ``proxy.connect((ip, port))`` — get a connected ``socket.socket``
  to wire into a paramiko ``sock=`` slot, ``requests`` adapter, or
  asyncio low-level code.
* ``proxy.forward((ip, port))`` — get a local ``(host, port)`` to
  point opaque clients at (``curl``, ``proxmoxer``, browsers, any
  third-party SDK that only knows ``host:port`` strings).

See :doc:`/usage/proxy` for cookbook-style usage.

Backends:

* :class:`SSHProxy` — paramiko ``Transport`` over OpenSSH-with-
  TCP-forwarding.  Used by libvirt, proxmox, and ESXi-7+ /
  Hyper-V-with-OpenSSH-Server.  Pre-7 ESXi and Hyper-V-without-
  OpenSSH need backend-specific fallbacks documented on the
  :class:`Proxy` ABC.
"""

from testrange.proxy.base import Proxy
from testrange.proxy.ssh import SSHProxy

__all__ = ["Proxy", "SSHProxy"]
