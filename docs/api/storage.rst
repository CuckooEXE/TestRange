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
  operations parameterised over a transport.  Each shipped backend
  contributes its own concrete subclass under
  ``testrange.backends.<backend>``; future formats plug in the same
  way.

Why two axes
------------

Every non-trivial TestRange feature comes back to the same
question: "where does this image live, and how do I manipulate
one?"  A local hypervisor is a filesystem path plus a local
subprocess; a remote one is SFTP + remote-exec of the same tool;
a different format on the same host is the same transport with a
different tool.  Decomposing into ``(transport, format)`` means
adding a new transport doesn't force every format to re-learn it,
and adding a new format doesn't force every transport to re-learn
it.

Call sites use the two axes explicitly::

    # File + exec primitives — transport concerns
    run.storage.transport.write_bytes(ref, data)
    run.storage.transport.upload(local_path, ref)

    # Disk / image primitives — format concerns
    run.storage.disk.create_overlay(backing_ref, dest_ref)
    run.storage.disk.resize(ref, "64G")

Shipped pairings
----------------

The generic storage layer here is **format-agnostic** — it ships
the composer (:class:`StorageBackend`), both transports
(:class:`LocalFileTransport`, :class:`SSHFileTransport`), and the
disk-format ABC.  Concrete disk-format implementations and any
pre-composed pairings live in their owning backend module under
``testrange.backends.<backend>`` because the disk-format binding
is what pins a pairing to a specific hypervisor family.

See each backend module (:doc:`backends`) for its own pre-composed
``<Backend>LocalStorageBackend`` /
``<Backend>SSHStorageBackend`` etc.

Callers that need a custom pairing (different transport × different
disk format) build a :class:`~testrange.storage.StorageBackend`
directly.

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

Concrete disk-format implementations live in each backend module —
see :doc:`backends`.

Composition
-----------

.. autoclass:: testrange.storage.StorageBackend
   :members:
   :show-inheritance:
