Cache
=====

Four classes of artifact live in the TestRange cache:

1. **Base cloud images** — raw distro qcow2/img files downloaded once
   from upstream (cloud.debian.org, cloud-images.ubuntu.com, …).
   Keyed by SHA-256 of the source URL.  The heavy downloads.

2. **Staged local ISOs** — large install media (e.g. Windows ISOs)
   copied into the cache so they live at a stable, cache-managed
   location.  Keyed by SHA-256 of file contents; named
   ``iso-<hash>.iso``.  See
   :meth:`~testrange.cache.CacheManager.stage_local_iso`.

3. **virtio-win.iso** — signed Windows driver ISO from
   fedorapeople.  Stable filename (no URL hash).  See
   :meth:`~testrange.cache.CacheManager.get_virtio_win_iso`.

4. **Post-install VM snapshots** — compressed qcow2 files produced by
   running cloud-init (Linux) or Windows Setup + autounattend
   (Windows) on a base image once.  Keyed by
   :func:`~testrange.cache.vm_config_hash`, which folds in the iso,
   user list, package list, post-install commands, and disk size.
   Subsequent runs of an identically-spec'd VM hit this cache and
   skip the install phase entirely.  A fifth variant — content-hash
   keyed ``byoi-<hash>.qcow2`` — holds
   :class:`~testrange.vms.builders.NoOpBuilder`-staged user images.

Each cached VM has a sibling ``<hash>.json`` manifest describing
exactly what went into it — pop it open in any JSON viewer and you
can see which packages were installed, which users were created, and
which post-install commands ran, without booting anything.

Cache location
--------------

The default root is ``/var/tmp/testrange/<user>``.  Override with
the ``TESTRANGE_CACHE_DIR`` environment variable.  See
:doc:`/usage/installation` for the permission requirements on a
custom path.

Inspecting the cache
--------------------

Two CLI commands:

- ``testrange cache-list`` — enumerate base images and installed VMs.
- ``testrange cache-clear`` — delete the VM snapshot cache (base
  images are left alone so you don't re-download distros).

Design notes
------------

**Download locks are per-URL.**  Concurrent orchestrators downloading
the same image serialise on a ``<hash>.lock`` file; the losers wait
up to 30 minutes for the winner to finish and then hit the cache.

**VM storage is compressed.**  Post-install images go through
``qemu-img convert -c`` so the 2-3 GiB working disk typically
compresses to 300-500 MiB.

**SSH keys are excluded from the hash.**  Rotating SSH keys would
otherwise invalidate every cached VM.  Instead, keys are injected
fresh in phase 2 (the per-run seed ISO) so the same cached image
serves every team member without rebuilds.

Reference
---------

.. autoclass:: testrange.cache.CacheManager
   :members:
   :show-inheritance:

.. autofunction:: testrange.cache.vm_config_hash
