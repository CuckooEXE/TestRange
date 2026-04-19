"""Hypervisor backends for TestRange.

The default libvirt / KVM / QEMU backend lives at module-level paths
(``testrange.orchestrator``, ``testrange.vms.libvirt``,
``testrange.networks.libvirt``) for backward compatibility.  Future
backends slot in as subpackages here.

Current members:

- :mod:`testrange.backends.proxmox` — Proxmox VE scaffolding.  Not yet
  implemented; importing succeeds but instantiation raises
  :class:`NotImplementedError`.
"""
