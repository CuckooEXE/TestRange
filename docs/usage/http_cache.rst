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

Wiring it into TestRange
------------------------

Pass ``cache=`` to the orchestrator constructor and the per-host
:doc:`local cache <caching>` will use it as a second-tier fill
source:

.. code-block:: python

   from testrange import Orchestrator

   with Orchestrator(
       cache="https://cache.testrange",
       cache_verify=False,           # bundled docker uses self-signed
       networks=[...],
       vms=[...],
   ) as orch:
       ...

Behaviour
~~~~~~~~~

* **Base images** (``get_image``).  Local hit short-circuits as
  before.  Local miss → check remote → if remote hit, ``GET`` to
  local cache + synthesise a meta sidecar.  Remote miss → download
  from upstream URL → ``PUT`` to remote on success.
* **VM snapshots** (``get_vm`` / ``store_vm``).  Local hit
  short-circuits.  Local miss → ``GET`` qcow2 + manifest from
  remote and land them in the local backend.  ``store_vm`` after
  install ``PUT``\ s both back to the remote.
* **Failure handling.**  Every remote operation is best-effort.
  Connection errors, timeouts, and unexpected status codes log a
  warning and fall through to the cold path; a flaky cache slows
  test runs back to local-cache-only speeds, never breaks them.
* **Backend scope.**  Remote fill / publish runs only when the
  backend's storage transport is local (the default).  SSH-reached
  hypervisors are skipped to avoid round-tripping multi-GiB
  artifacts through the test runner — put the cache and the
  hypervisor on the same network if you want them sharing.

URL keyspace
~~~~~~~~~~~~

VM artifacts live under one directory per cached VM, prefixed by
the hypervisor backend so a single remote can serve multiple
backends without artifact-format collisions::

    <backend>/vms/<config_hash>/disk.qcow2     # primary disk
    <backend>/vms/<config_hash>/manifest.json  # build manifest
    <backend>/vms/<config_hash>/nvram.fd       # UEFI vars (when applicable)
    <backend>/vms/<config_hash>/disk-1.qcow2   # additional drives, if any

Where ``<backend>`` is ``libvirt``, ``proxmox``, eventually
``hyperv`` — the orchestrator's
:meth:`~testrange.orchestrator_base.AbstractOrchestrator.backend_type`.
This mirrors the per-VM-directory layout TestRange uses on the
local filesystem (see :doc:`caching`); the backend prefix is added
to the URL because the local layout is already namespaced by each
backend's storage transport.

Base images sit at the top level — they're upstream-keyed and
backend-agnostic::

    images/<url_hash>.qcow2 (or .img)
    images/<url_hash>.meta.json

``url_hash`` and ``config_hash`` are the same 24-char SHA-256
prefixes the local cache uses (see
:func:`testrange.cache.vm_config_hash`), so artifacts produced by
one host can be picked up unchanged by any other host that asks
for the same URL or VM spec.

Not yet on the remote
~~~~~~~~~~~~~~~~~~~~~

* **NVRAM** (``<config_hash>/nvram.fd``) — UEFI installs still
  cache it locally; a follow-up will mirror it as a peer resource
  alongside ``disk.qcow2``.
* **virtio-win.iso** and **staged Windows ISOs** — fetched
  upstream / staged locally as today.
* **Proxmox prepared ISOs** — local-only; PVE-side caching is
  a separate slice.
