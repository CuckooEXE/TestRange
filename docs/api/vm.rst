Virtual Machines
================

A VM in TestRange is a **spec** at construction time (name, image,
users, packages, devices) and a **runtime handle** once the
orchestrator has booted it.  The same Python object represents both —
methods that need a live VM (``exec``, ``hostname``, the file helpers,
``shutdown``) raise :class:`~testrange.exceptions.VMNotRunningError`
if called before provisioning.

Building the spec
-----------------

:class:`~testrange.backends.libvirt.VM` takes a small set of keyword
arguments:

- ``name`` — unique per test run; shows up as hostname, in DNS, and in
  libvirt domain names.
- ``iso`` — an absolute path to a local qcow2/img, or an ``https://``
  URL pointing at an upstream cloud image.  See :doc:`/usage/vms` for
  common upstream URLs.
- ``users`` — at least one :class:`~testrange.credentials.Credential`
  must be ``username='root'``.  Additional users can be non-sudo or
  passwordless-sudo.
- ``pkgs`` — list of :class:`~testrange.packages.AbstractPackage`
  instances.  Honoured by
  :class:`~testrange.vms.builders.CloudInitBuilder` (apt/dnf go into
  ``packages:``, pip/brew into ``runcmd``) and
  :class:`~testrange.vms.builders.WindowsUnattendedBuilder` (winget
  only); silently ignored by
  :class:`~testrange.vms.builders.NoOpBuilder`.
- ``post_install_cmds`` — one-liners run after package installation
  during the install phase.  Shell on Linux (cloud-init
  ``runcmd``), PowerShell on Windows (autounattend
  ``FirstLogonCommands``).  Ignored by :class:`NoOpBuilder`.
- ``devices`` — composable list of vCPU, Memory, HardDrive, and
  VirtualNetworkRef entries.  Order doesn't matter; defaults apply to
  anything omitted (2 vCPU, 2 GiB RAM, 20 GB disk, no NICs).
- ``builder`` — explicit
  :class:`~testrange.vms.builders.base.Builder` strategy.  When
  ``None`` (the default) a builder is auto-selected from ``iso``:
  Windows install ISOs →
  :class:`~testrange.vms.builders.WindowsUnattendedBuilder`,
  everything else →
  :class:`~testrange.vms.builders.CloudInitBuilder`.  Pass
  :class:`~testrange.vms.builders.NoOpBuilder` explicitly for prebuilt
  qcow2 images (see :ref:`BYOI <byoi>`).
- ``communicator`` — which backend the orchestrator wires up once the
  domain is running: ``"guest-agent"``, ``"ssh"``, or ``"winrm"``.
  When ``None`` (the default) the builder's
  :meth:`~testrange.vms.builders.base.Builder.default_communicator`
  picks one.  See :doc:`/usage/communication` for the selection rules.

Which install path runs depends on the builder:

- :class:`~testrange.vms.builders.CloudInitBuilder` (default for
  ``.qcow2`` / ``.img`` / ``https://``) → cloud-init install phase,
  outputs a cached post-install qcow2.
- :class:`~testrange.vms.builders.WindowsUnattendedBuilder` (default
  for Windows install ISOs detected by
  :func:`~testrange.vms.images.is_windows_image`) → Windows Setup
  driven by autounattend, OVMF firmware, SATA primary disk, e1000e
  NIC, ``virtio-win.iso`` for drivers, outputs a cached installed
  qcow2.  See :doc:`/usage/windows` for the walkthrough.
- :class:`~testrange.vms.builders.NoOpBuilder` — no install phase;
  the user-supplied qcow2 is staged into the cache by content hash.

The spec is deterministic: two VMs with identical user/package/cmd
lists map to the same cache hash and share a single compressed disk
image in the cache.  See
:meth:`~testrange.vms.builders.base.Builder.cache_key` and
:func:`~testrange.cache.vm_config_hash`.  ``NoOpBuilder``-backed VMs
skip the config hash entirely and key their cache on the content hash
of the user-supplied qcow2.

Talking to a running VM
-----------------------

All runtime calls go through a
:class:`~testrange.communication.base.AbstractCommunicator`.  For
Linux guests this is the QEMU guest agent (virtio-serial, no TCP);
Windows guests default to WinRM.  See :doc:`communication`.

The high-level methods on :class:`~testrange.vms.base.AbstractVM` are
thin wrappers that:

1. Assert the VM is running (``_require_communicator``).
2. Delegate to the communicator's primitive (``exec``, ``get_file``,
   ``put_file``, ``hostname``).

File helpers
~~~~~~~~~~~~

Four convenience wrappers sit on top of the bytes-in/bytes-out
primitives so tests don't have to hand-encode every string or
juggle ``open()``:

- :meth:`~testrange.vms.base.AbstractVM.read_text` /
  :meth:`~testrange.vms.base.AbstractVM.write_text` — UTF-8 by
  default, encoding is a keyword argument.
- :meth:`~testrange.vms.base.AbstractVM.download` — copy a file from
  the VM to the host; auto-creates the destination's parent
  directory and returns the resolved :class:`~pathlib.Path`.
- :meth:`~testrange.vms.base.AbstractVM.upload` — copy a host file
  into the VM; raises :class:`FileNotFoundError` before touching the
  VM if the local file is missing.

These are safe to layer because they go through the same gated
``_require_communicator()`` path as the primitives.

Nested: Hypervisor VMs
----------------------

A :class:`~testrange.Hypervisor` is a VM that also drives an **inner**
orchestrator.  It carries three extra fields on top of the plain
:class:`~testrange.VM` surface:

- ``orchestrator`` — an
  :class:`~testrange.orchestrator_base.AbstractOrchestrator` *class*
  (not an instance).  The outer orchestrator calls this class's
  :meth:`~testrange.orchestrator_base.AbstractOrchestrator.root_on_vm`
  once the hypervisor VM is booted to produce an inner orchestrator
  rooted on the VM.
- ``vms`` — :class:`~testrange.VM` specs for the inner layer.
- ``networks`` — :class:`~testrange.VirtualNetwork` specs for the
  inner layer.

The libvirt :class:`Hypervisor` concrete class additionally pre-loads
``libvirt-daemon-system``, ``qemu-kvm``, and ``qemu-utils`` via apt,
and adds ``systemctl enable --now libvirtd`` plus ``usermod -aG
libvirt,kvm`` for each declared user to ``post_install_cmds`` —
enough for the nested ``qemu+ssh://`` URI to connect and drive the
inner layer.  Caller-supplied ``pkgs`` / ``post_install_cmds`` are
appended, so the library's steps run first.

Prerequisites and cross-layer behaviour are documented in
:doc:`/usage/installation`.

Reference
---------

.. autoclass:: testrange.backends.libvirt.VM
   :members:
   :show-inheritance:

.. autoclass:: testrange.vms.base.AbstractVM
   :members:
   :show-inheritance:

.. autoclass:: testrange.backends.libvirt.Hypervisor
   :members:
   :show-inheritance:

.. autoclass:: testrange.vms.hypervisor_base.AbstractHypervisor
   :members:
   :show-inheritance:

.. autoclass:: testrange.credentials.Credential
   :members:
   :show-inheritance:

.. autofunction:: testrange.vms.images.resolve_image

.. autofunction:: testrange.vms.images.is_windows_image
