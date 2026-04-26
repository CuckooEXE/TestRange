Caching
=======

TestRange caches two kinds of artifact so test runs stay fast even
when they provision complex VMs.

What gets cached
----------------

**Base images.**  The first time a test requests an ``https://``
``iso=``, TestRange downloads the upstream cloud image into
``<cache_root>/images/<url_hash>.qcow2``.  Every subsequent VM that
uses the same URL skips the download.

**Post-install VM snapshots.**  After a VM's install phase runs
(packages installed, users created, post-install commands executed),
the resulting disk is compressed with ``qemu-img convert -c`` and
stored under a per-VM directory at
``<cache_root>/vms/<config_hash>/``.  Each cached VM owns a
directory of *resources* — two universal ones (the primary disk
and the manifest) plus any backend-specific files the hypervisor
keeps alongside them::

    <cache_root>/vms/<config_hash>/
    ├── disk.qcow2          # primary post-install disk
    ├── manifest.json       # build manifest (what installed)
    └── ...                 # backend-specific resources

Backends drop additional files in the same directory using
:meth:`~testrange.cache.CacheManager.vm_resource_ref` with an
arbitrary name — additional drives (``disk-1.qcow2``, ``disk-2.qcow2``…),
hypervisor-specific config blobs, firmware-state snapshots — without
the cache layer needing to know what they mean.  The
``manifest.json`` records exactly what went into the image; open
it in any JSON viewer to audit a cached build.

**Staged prebuilt (BYOI) images.**  When a VM is declared with
``builder=NoOpBuilder()`` the source qcow2 is content-hashed and
copied to ``<cache_root>/vms/byoi-<sha256[:24]>.qcow2`` on first use.
Files that already live inside the cache root are used in place.
The sibling ``byoi-<sha256[:24]>.json`` records the source path and
hash for auditing.  See :doc:`vms` for the full BYOI rules.

**Staged Windows install ISOs.**  On first use, TestRange
content-hashes and copies the Windows ISO to
``<cache_root>/images/iso-<sha256[:24]>.iso``.  Re-runs reuse the
staged copy.  See :doc:`windows` for the install flow.

**virtio-win.iso.**  The signed virtio driver ISO from
``fedorapeople.org/groups/virt/virtio-win`` is downloaded lazily the
first time a Windows VM builds and cached at
``<cache_root>/images/virtio-win.iso``.  Subsequent Windows builds
are fully offline.  Roughly 800 MiB.

When the cache hits
-------------------

The snapshot cache key is a SHA-256 of (sorted) inputs:

- The ``iso=`` string (URL or absolute path)
- The sorted ``(username, password, sudo)`` tuples — **SSH keys are
  deliberately excluded** so key rotation doesn't invalidate builds
- The sorted ``repr(pkg)`` strings for every declared package
- The ordered list of ``post_install_cmds``
- The primary-disk size (normalised to ``<n>G``)

Anything else — network layout, runtime IPs, hostnames, test
functions — is *not* part of the hash.  Two VMs with the same
software stack share the same compressed image even if they're used
in wildly different tests.

When the cache misses
---------------------

The install phase runs.  It takes minutes rather than seconds because
it actually installs packages.  Watch the ``testrange`` log — every
VM build emits a ``cache hit`` or ``cache miss`` line up front so you
know which path you're on.

Where the cache lives
---------------------

Default: ``/var/tmp/testrange/<user>/``.  Override with
``TESTRANGE_CACHE_DIR``.  Permission requirements for custom paths are
covered in :doc:`installation`.

CLI inspection
--------------

.. code-block:: bash

    # List every base image and cached VM, with sizes and summaries
    testrange cache-list

    # Drop all post-install snapshots (keeps base images so distros
    # aren't re-downloaded)
    testrange cache-clear

Design notes
------------

**Base-image downloads are locked per-URL.**  Concurrent test runs
requesting the same image serialise on a ``<hash>.lock`` file so you
don't fetch the same 500 MiB qcow2 twice.

**Snapshots are compressed.**  A post-install Debian working disk is
2-3 GiB; qemu-img's cluster-level compression typically shrinks it to
300-500 MiB.  This trades a few seconds of CPU during the install
phase for meaningful disk savings.

**Integrity is sha256-verified on download.**  The ``<hash>.meta.json``
sidecar records the content hash and byte size; corrupted downloads
are detected on the next resolver call.
