HTTP Cache
==========

The ``cache/`` directory at the repo root ships a self-contained
nginx + docker-compose stack that serves as a remote artifact
store.  It is **separate** from TestRange's per-host
:doc:`local cache <caching>`: the local cache lives on each test
runner's filesystem, while this HTTP cache is a service intended
to be shared across multiple hosts (and eventually multiple
hypervisor backends — libvirt, Proxmox, Hyper-V).

Scope
-----

The cache is deliberately a dumb blob store.  It exposes three
HTTP verbs and nothing else:

==========  =============================================  ==================
Verb        URL                                            Behaviour
==========  =============================================  ==================
``GET``     ``https://<host>/<path>``                      Read a stored blob
``PUT``     ``https://<host>/<path>``                      Write a blob from
                                                           the request body;
                                                           creates parent
                                                           directories
``DELETE``  ``https://<host>/<path>``                      Remove a blob
==========  =============================================  ==================

Anything else (``POST``, ``HEAD``, ``OPTIONS``, ``MKCOL``…) is
rejected with ``403``.  Plain HTTP on port 80 redirects to HTTPS
on 443.

Why ``PUT``, not ``POST``
~~~~~~~~~~~~~~~~~~~~~~~~~

nginx's built-in ``ngx_http_dav_module`` handles file uploads
natively via ``PUT`` (the URL path is the destination).  Supporting
``POST`` would require either an upstream application server or
``client_body_in_file_only`` plus ``rewrite`` directives — both of
which obscure the config.  ``PUT`` is the WebDAV / HTTP-spec verb
for "store this representation at this URL," so it matches the
intent.

Running it
----------

.. code-block:: bash

   cd cache/
   docker compose up -d

On first start, ``docker-init/10-gen-certs.sh`` generates a
self-signed RSA-2048 cert into ``cache/certs/`` (10-year validity,
``CN=testrange-cache``) and ``apk add``\ s ``openssl`` into the
running ``nginx:alpine`` image to do so.  Subsequent starts reuse
the existing cert.  The certs and stored artifacts are bind-mounted
into the host:

============================  ==============================================
``cache/certs/``              Self-signed cert + key (gitignored)
``cache/storage/``            All stored blobs, mirroring URL paths
                              (gitignored)
============================  ==============================================

Replace ``cache/certs/server.{crt,key}`` to install your own cert,
or wipe the directory and restart the container to regenerate.

Examples
--------

.. code-block:: bash

   # Store a file (-k accepts the self-signed cert)
   curl -kT post-install-debian-12.qcow2 \
       https://cache.example/vms/abc123.qcow2

   # Fetch it
   curl -k -o local.qcow2 https://cache.example/vms/abc123.qcow2

   # Delete it
   curl -k -X DELETE https://cache.example/vms/abc123.qcow2

PUT into a path whose parent directories don't exist yet just
works — ``create_full_put_path on`` mirrors ``mkdir -p`` semantics
on the server side.

What's *not* in scope
---------------------

* **Authentication.**  The cache is currently open.  Run it on a
  trusted network or layer ``auth_basic`` / mTLS into ``nginx.conf``
  if you need access control.
* **Eviction.**  No quota, no TTL, no LRU.  Storage grows until
  the operator deletes blobs.  Pair with a ``find -mtime`` cron
  if that becomes a problem.
* **Range requests / resume.**  Stock nginx serves ``Range``
  headers on ``GET`` for free; ``PUT`` is all-or-nothing.
* **Content addressing.**  Callers choose the URL path.  Hashing
  conventions (e.g. ``vms/<config_hash>.qcow2``) are TestRange's
  responsibility, not the cache's.

Future TestRange integration
----------------------------

Once wired in, the local :doc:`caching <caching>` layer will check
the HTTP cache before doing a fresh install and ``PUT`` the
resulting artifact back on success.  Each backend keeps its native
on-disk format on the cache (qcow2 for libvirt, ``.vma.zst`` for
Proxmox, vhdx for Hyper-V) — the HTTP layer doesn't care; it's
just bytes keyed by URL.
