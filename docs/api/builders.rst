Builders
========

Every :class:`~testrange.backends.libvirt.VM` owns a
:class:`~testrange.vms.builders.base.Builder` that encodes the
provisioning strategy — how the VM gets from ``iso=`` to a runnable
disk image.  :class:`VM` delegates everything install-pipeline related
to the builder: disk preparation, seed ISO generation, domain-XML
hints (UEFI, Windows device models, extra CD-ROMs), and cache key
derivation.

Why a strategy object?
----------------------

Before the refactor, :class:`VM.build` was a chain of ``if
self.prebuilt / self._is_windows`` branches, and
:class:`VM.start_run` carried its own twin set of those branches.
Adding a new install flavour (preseed, Kickstart, sysprep'd Windows)
meant threading another bool through every method.

With the Builder strategy the branching lives in one place — the
builder — and :class:`VM` stays a thin spec-plus-XML renderer.
Subclass :class:`~testrange.vms.builders.base.Builder` and plug it in
via ``builder=`` at construction time; :class:`VM` doesn't care what
kind of install runs underneath.

The contract
------------

Builders are **stateless**: one instance can serve any number of VMs.
All per-VM state comes in through the
:class:`~testrange.backends.libvirt.VM` argument of each method.  The ABC
defines eight methods:

- :meth:`~testrange.vms.builders.base.Builder.default_communicator` —
  which transport to attach when the caller does not pass
  ``communicator=``.
- :meth:`~testrange.vms.builders.base.Builder.needs_install_phase` —
  whether :meth:`VM.build` should boot a one-off install domain.
  ``False`` for :class:`NoOpBuilder`-style "image already ready"
  strategies.
- :meth:`~testrange.vms.builders.base.Builder.needs_boot_keypress` —
  whether the install domain's early boot needs spacebars spammed
  into it to consume a "Press any key" prompt.  ``True`` for
  :class:`WindowsUnattendedBuilder` under UEFI; ``False`` elsewhere.
- :meth:`~testrange.vms.builders.base.Builder.cache_key` — hash under
  which the post-install disk is cached.
- :meth:`~testrange.vms.builders.base.Builder.prepare_install_domain`
  — produce the :class:`InstallDomain` (primary disk, seed ISO, extra
  CD-ROMs, domain-XML hints) the orchestrator needs to boot the
  install.
- :meth:`~testrange.vms.builders.base.Builder.install_manifest` —
  JSON-serialisable metadata for the cached disk's sidecar manifest.
- :meth:`~testrange.vms.builders.base.Builder.prepare_run_domain` —
  :class:`RunDomain` hints for each test run (seed ISO, firmware,
  device models).
- :meth:`~testrange.vms.builders.base.Builder.ready_image` — for
  no-install builders only; return the path to a disk that is already
  usable.

Concrete builders
-----------------

- :class:`~testrange.vms.builders.CloudInitBuilder` — Linux cloud
  images.  Boots a NoCloud seed ISO on an overlay of the resolved
  base image; phase-2 seed rotates instance-id on every run.
- :class:`~testrange.vms.builders.WindowsUnattendedBuilder` — Windows
  install ISOs.  Generates an autounattend answer file, attaches the
  Windows ISO plus ``virtio-win.iso`` as CD-ROMs, runs Setup under
  OVMF/UEFI.  See :doc:`/usage/windows`.
- :class:`~testrange.vms.builders.NoOpBuilder` — prebuilt qcow2
  (BYOI).  No install phase; the image is content-hash staged into
  the cache and booted from an overlay.  See :ref:`BYOI <byoi>`.

Reference
---------

.. autoclass:: testrange.vms.builders.base.Builder
   :members:
   :show-inheritance:

.. autoclass:: testrange.vms.builders.base.InstallDomain
   :members:

.. autoclass:: testrange.vms.builders.base.RunDomain
   :members:

.. autoclass:: testrange.vms.builders.CloudInitBuilder
   :members:
   :show-inheritance:

.. autoclass:: testrange.vms.builders.WindowsUnattendedBuilder
   :members:
   :show-inheritance:

.. autoclass:: testrange.vms.builders.NoOpBuilder
   :members:
   :show-inheritance:

.. autofunction:: testrange.vms.builders.cloud_init.write_seed_iso

.. autofunction:: testrange.vms.builders.unattend.write_autounattend_iso
