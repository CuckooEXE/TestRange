Backends
========

TestRange separates the *what* of a test run (VM specs, networks,
builders) from the *how* — the hypervisor backend that realises those
specs.  The default backend drives KVM/QEMU via libvirt; future
backends slot in alongside it by implementing a small set of
abstract base classes.

Abstract contracts
------------------

Four ABCs describe everything a backend has to implement.  Each
concrete backend lives under :mod:`testrange.backends` as its own
subpackage:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - ABC
     - Lives in
     - Libvirt impl
   * - :class:`~testrange.orchestrator_base.AbstractOrchestrator`
     - ``testrange.orchestrator_base``
     - :class:`~testrange.backends.libvirt.Orchestrator` (alias
       :class:`~testrange.LibvirtOrchestrator`)
   * - :class:`~testrange.vms.base.AbstractVM`
     - ``testrange.vms.base``
     - :class:`~testrange.backends.libvirt.VM`
   * - :class:`~testrange.networks.base.AbstractVirtualNetwork`
     - ``testrange.networks.base``
     - :class:`~testrange.backends.libvirt.VirtualNetwork`
   * - :class:`~testrange.communication.base.AbstractCommunicator`
     - ``testrange.communication.base``
     - :class:`~testrange.backends.libvirt.GuestAgentCommunicator`,
       :class:`~testrange.communication.ssh.SSHCommunicator`,
       :class:`~testrange.communication.winrm.WinRMCommunicator`

The abstract VM and network methods take the orchestrator as a
``context`` argument; backends downcast to pick up their native
handle (a ``libvirt.virConnect``, a proxmoxer client, …).  The
:class:`~testrange.vms.builders.base.Builder` contract
(:class:`InstallDomain` / :class:`RunDomain` dataclasses) is the
hypervisor-neutral bridge between builders and backends — every
backend consumes the same builder outputs and renders them into
its native representation.

The libvirt backend
-------------------

The default.  Targets ``qemu:///system`` (or any URI libvirt
accepts — ``qemu+ssh://user@host/system`` works too).  Generates
libvirt domain XML, manages libvirt networks (dnsmasq-backed
bridges), uses the QEMU guest agent through a virtio-serial
channel.  See :doc:`/usage/vms` and :doc:`/usage/windows` for
user-facing docs; the concrete classes are
:class:`~testrange.backends.libvirt.Orchestrator`,
:class:`~testrange.backends.libvirt.VM`,
:class:`~testrange.backends.libvirt.VirtualNetwork`.

The top-level package symbols (``testrange.Orchestrator``,
``testrange.VM``, ``testrange.VirtualNetwork``) resolve to these
libvirt implementations.  The explicit alias
:class:`~testrange.LibvirtOrchestrator` is available for code that
wants to be unambiguous about which backend it's asking for.

The Proxmox backend
-------------------

**Status: scaffolding only.**  The classes are importable and the
abstract contracts are satisfied, but instantiating
:meth:`~testrange.backends.proxmox.ProxmoxOrchestrator.__enter__`
raises :class:`NotImplementedError` with a message pointing at the
work still to do.

Reachable as:

.. code-block:: python

   from testrange.backends.proxmox import (
       ProxmoxOrchestrator,
       ProxmoxVM,
       ProxmoxVirtualNetwork,
   )

The TODO list lives in the module docstring of
:mod:`testrange.backends.proxmox.orchestrator`.  Summary: authenticate
via REST (``proxmoxer``), create SDN vnets for each network, upload
disk images into the configured storage pool, drive VM lifecycle via
``POST /nodes/{node}/qemu`` + ``status/start`` / ``status/stop``.

No top-level export — ``testrange.Orchestrator`` stays libvirt until
the Proxmox backend is implemented, so nobody accidentally dispatches
against a stub.

Adding a new backend
--------------------

Checklist for a new hypervisor (e.g. VMware, OpenNebula):

1. Create ``testrange/backends/<name>/`` with ``__init__.py``,
   ``orchestrator.py``, ``vm.py``, ``network.py``, and (if the
   backend ships its own guest-agent protocol) ``guest_agent.py``.
2. Subclass :class:`AbstractOrchestrator`, :class:`AbstractVM`,
   :class:`AbstractVirtualNetwork`.  Implement their abstract methods
   — the signatures are already portable (``context:
   AbstractOrchestrator`` carries whatever handle your backend
   needs).  Override
   :meth:`~testrange.orchestrator_base.AbstractOrchestrator.backend_type`
   to return a short string identifier.
3. Consume :class:`~testrange.vms.builders.base.InstallDomain` and
   :class:`~testrange.vms.builders.base.RunDomain` in your VM's
   ``build`` / ``start_run`` — every builder shipped with TestRange
   already produces these; your backend only translates them into
   native calls.
4. **If your platform has its own guest-agent protocol**, write a
   ``*GuestAgentCommunicator`` and override
   :meth:`~testrange.vms.base.AbstractVM._make_guest_agent_communicator`
   on your VM subclass to return it.  SSH and WinRM communicators
   are shared across backends and work unchanged (the ABC handles
   them in :meth:`~testrange.vms.base.AbstractVM._make_communicator`).
5. Register the package-level alias in ``testrange/__init__.py``
   once the backend is production-ready.

Adding a new guest-OS install flavour
-------------------------------------

Builders (cloud-init, Windows autounattend) are *separate* from the
backend: the same builder runs against every hypervisor.  To support
a new OS install flow — Debian preseed, RHEL Kickstart, Alpine
apkovl, sysprep'd Windows bundles — subclass
:class:`~testrange.vms.builders.base.Builder`, then register a
predicate so :class:`VM` picks it up automatically:

.. code-block:: python

    from testrange.vms.builders import Builder, register_builder

    class DebianPreseedBuilder(Builder):
        ...

    def is_debian_installer_iso(iso: str) -> bool:
        return iso.endswith(".iso") and "debian" in iso.lower()

    register_builder(is_debian_installer_iso, DebianPreseedBuilder)

Earlier registry entries win; ``register_builder(..., prepend=False)``
drops your entry to the fallback slot so it only fires when no
other predicate matches.  The default registry ships with the
Windows-ISO check; everything that doesn't match any predicate falls
through to :class:`~testrange.vms.builders.CloudInitBuilder`.  See
:func:`~testrange.vms.builders.auto_select_builder` and
:data:`~testrange.vms.builders.BUILDER_REGISTRY`.

Reference
---------

.. autoclass:: testrange.orchestrator_base.AbstractOrchestrator
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.proxmox.ProxmoxOrchestrator
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.proxmox.ProxmoxVM
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.proxmox.ProxmoxVirtualNetwork
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.proxmox.ProxmoxGuestAgentCommunicator
   :members:
   :show-inheritance:

.. autofunction:: testrange.vms.builders.auto_select_builder

.. autofunction:: testrange.vms.builders.register_builder
