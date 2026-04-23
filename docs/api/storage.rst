Storage
=======

A :class:`~testrange.storage.StorageBackend` is the glue between
"outer Python does orchestration logic" and "some host reads actual
disk bytes for the hypervisor."  It's decomposed along two
independent axes:

- :class:`~testrange.storage.AbstractFileTransport` — file +
  subprocess primitives against a filesystem (local, SSH-reachable
  remote, future REST-based).
- :class:`~testrange.storage.AbstractDiskFormat` — disk-image
  operations parameterised over a transport (qcow2 via ``qemu-img``
  today; VHDX via PowerShell, VMDK, Proxmox storage volumes are
  plug-ins of the same shape).

Why two axes
------------

Every non-trivial TestRange feature comes back to the same question:
"where does this image live, and how do I manipulate one?"  Local KVM
is a filesystem path + ``qemu-img`` subprocess.  Remote KVM is SFTP +
``ssh remote qemu-img``.  A future Hyper-V host is SMB / PSSession +
PowerShell ``New-VHD``.  Decomposing into ``(transport, format)``
means adding a new transport doesn't force every format to re-learn
it, and adding a new format doesn't force every transport to
re-learn it.

Call sites use the two axes explicitly::

    # File + exec primitives — transport concerns
    run.storage.transport.write_bytes(ref, data)
    run.storage.transport.upload(local_path, ref)

    # Disk / image primitives — format concerns
    run.storage.disk.create_overlay(backing_ref, dest_ref)
    run.storage.disk.resize(ref, "64G")

Shipped pairings
----------------

- :class:`~testrange.storage.LocalStorageBackend` — local filesystem
  + qcow2.  Default for ``Orchestrator(host="localhost")``.
- :class:`~testrange.storage.SSHStorageBackend` — SFTP/SSH + qcow2.
  Auto-selected for ``Orchestrator(host="qemu+ssh://...")``.

Callers that need a custom pairing build a
:class:`~testrange.storage.StorageBackend` directly with whichever
transport and format they want.

Transport axis
--------------

.. autoclass:: testrange.storage.AbstractFileTransport
   :members:
   :show-inheritance:

.. autoclass:: testrange.storage.LocalFileTransport
   :members:
   :show-inheritance:

.. autoclass:: testrange.storage.SSHFileTransport
   :members:
   :show-inheritance:

Disk-format axis
----------------

.. autoclass:: testrange.storage.AbstractDiskFormat
   :members:
   :show-inheritance:

.. autoclass:: testrange.storage.Qcow2DiskFormat
   :members:
   :show-inheritance:

Composition
-----------

.. autoclass:: testrange.storage.StorageBackend
   :members:
   :show-inheritance:
