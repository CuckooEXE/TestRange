Architecture
============

TestRange's design philosophy in one sentence: **the entire project
is abstract except in the backends**.  Generic code talks to
abstract bases and protocols; concrete implementations of "how to
talk to <hypervisor>" all live under :mod:`testrange.backends`.

This page walks the layers.  If you're confused why there's both a
"cache" and a "rundir" and a "storage backend" and an "HTTP cache,"
you're in the right place.

Terminology
-----------

.. glossary::

   Backend
       A concrete hypervisor integration.  Lives at
       ``testrange.backends.<name>``.  Each backend module exposes
       its own ``<Backend>VM``, ``<Backend>Orchestrator``,
       ``<Backend>VirtualNetwork``, etc., all subclassing the
       abstract bases.  Generic code never imports from a backend
       directly; users do.  See :doc:`/api/backends` for the
       shipped backend list.

   Generic class
       A backend-agnostic implementation that any backend accepts.
       Examples: :class:`~testrange.GenericVM`,
       :class:`~testrange.HardDrive`,
       :class:`~testrange.vNIC`.  Carries only the universal fields;
       the orchestrator translates it into the backend's native
       type at provisioning time.

   Backend-specific class
       A concrete subclass of a sealed abstract base, exposing
       backend-specific knobs (bus selection, storage pool, cache
       mode, …).  Backend-specific classes are **siblings** of the
       generic — both extend the same abstract base directly.  That
       sibling-not-child relationship is what makes the type
       checker reject one backend's class being passed to another's
       VM.

   Sealed abstract base
       The abstract class that backend-specific siblings extend
       (:class:`~testrange.devices.AbstractHardDrive`,
       :class:`~testrange.vms.base.AbstractVM`, etc.).  "Sealed"
       because subclasses are intended to be siblings, not a long
       inheritance chain — the type system relies on the sibling
       structure.

   Builder
       Strategy that turns a VM spec into a runnable disk image.
       :class:`~testrange.CloudInitBuilder` for Linux cloud images,
       :class:`~testrange.WindowsUnattendedBuilder` for Windows
       installer ISOs, :class:`~testrange.NoOpBuilder` for
       prebuilt disks.  Backend-neutral — any backend's ``build()``
       consumes the same :class:`~testrange.vms.builders.base.InstallDomain`
       output.

   Communicator
       Runtime channel into a started VM.  A host-mediated
       guest-agent socket, SSH, or WinRM.  ``vm.exec(...)``,
       ``vm.get_file(...)``, etc. all delegate through the active
       communicator.

   Orchestrator
       Owner of one test run's resources.  Constructs networks +
       VMs at ``__enter__``, hands them to the test function, tears
       everything down at ``__exit__``.  Each backend has its own
       concrete orchestrator; :class:`testrange.Orchestrator`
       aliases the default-backend orchestrator (see
       :doc:`/api/backends`).

Storage and caching
-------------------

There are four pieces here, each with a distinct job.  This is
where most of the "what is this?" questions cluster.

::

    ┌────────────────────────────────────────────────────────────────┐
    │  StorageBackend  (transport + disk_format)                     │
    │  ──────────────                                                │
    │  "How do I read+write bytes for the hypervisor?"               │
    │  Used by BOTH CacheManager and RunDir.                         │
    │  Pre-composed pairings live in their backend module.           │
    └────────────────────────────────────────────────────────────────┘
                │ underneath ↓               │ underneath ↓
    ┌──────────────────────────┐    ┌──────────────────────────┐
    │  CacheManager            │    │  RunDir                  │
    │  ──────────────          │    │  ──────────              │
    │  PERSISTENT artifacts    │    │  EPHEMERAL artifacts     │
    │  Outlives every run      │    │  One per orchestrator    │
    │  Keyed by hash:          │    │  entry; deleted on exit  │
    │   - <root>/images/       │    │  <root>/runs/<run_id>/   │
    │   - <root>/vms/<hash>/   │    │  Holds:                  │
    │  Hit/miss skips install  │    │   - per-VM overlay disks │
    │  phase entirely          │    │   - install scratch disks│
    └────────┬─────────────────┘    │   - seed ISOs            │
             │                      │   - firmware state       │
             │ optional fill        │     (UEFI NVRAM, …)      │
             ↓ source               └──────────────────────────┘
    ┌──────────────────────────┐
    │  HTTP cache              │
    │  ──────────              │
    │  REMOTE second tier      │
    │  Shared across hosts     │
    │  Same content layout as  │
    │  local CacheManager,     │
    │  prefixed by backend     │
    │  name in the URL         │
    │  (<backend>/vms/<hash>/.)│
    └──────────────────────────┘

What each one does
~~~~~~~~~~~~~~~~~~

:class:`~testrange.StorageBackend` — **the I/O glue**

   A ``(transport, disk_format)`` pair.  The transport answers
   "where do these bytes live and how do I run commands against
   them?"; the disk format answers "what's in the bytes and what
   tools manipulate them?"  Decomposing into two axes means a new
   transport doesn't force every disk format to re-learn it, and
   vice versa.

   Transports: :class:`~testrange.storage.LocalFileTransport`
   (outer-host filesystem),
   :class:`~testrange.storage.SSHFileTransport` (SFTP + remote
   exec).

   Disk formats: each backend ships its own concrete
   :class:`~testrange.storage.AbstractDiskFormat` implementation
   under ``testrange.backends.<backend>``.

   **Pre-composed convenience pairings are backend-flavoured** —
   each backend that ships pre-composed pairings publishes them in
   its own backend module so the format binding lines up with the
   backend's native disk format.

:class:`~testrange.cache.CacheManager` — **the persistent cache**

   Lives at ``<cache_root>/`` (default
   ``/var/tmp/testrange/<user>/``).  Two kinds of artifact:

   - ``images/<url_hash>.<ext>`` — base images downloaded from
     upstream URLs.  Heavy.  Keyed by the source URL.
   - ``vms/<config_hash>/`` — post-install snapshots.  Each cached
     VM owns a directory of resources (disk + manifest + backend-
     specific extras).  Keyed by a hash over ``(iso, users,
     packages, post-install commands, disk size)``.

   Outlives every test run.  Hit on the second run skips the
   install phase entirely and overlays a fresh COW disk on the
   cached image.  See :doc:`caching` for details.

:class:`~testrange._run.RunDir` — **per-run scratch**

   Lives at ``<cache_root>/runs/<run_id>/``.  Holds files that
   exist only for the duration of one orchestrator entry:

   - ``<vm>{disk_extension}`` — per-VM COW overlay on the cached
     disk
   - ``<vm>-install{disk_extension}`` — install-phase working disk
     (cloud-init / autounattend writes here)
   - ``<vm>-install-seed.iso`` / ``<vm>-seed.iso`` — cloud-init
     seed ISOs (built by :class:`~testrange.CloudInitBuilder`)
   - ``<vm>-unattend.iso`` — Windows autounattend seed (built by
     :class:`~testrange.WindowsUnattendedBuilder`)
   - per-run firmware-state files (e.g. UEFI NVRAM) — each backend
     composes its own filenames via
     :meth:`~testrange._run.RunDir.path_for`

   Created at ``__enter__``, deleted at ``__exit__``.  When a test
   process is killed (``kill -9``, OOM, host reboot) the dir is
   left behind — that's what ``testrange cleanup MODULE RUN_ID``
   recovers from.

:doc:`HTTP cache <http_cache>` — **the optional remote tier**

   An nginx server somewhere on the network, populated and consumed
   by ``CacheManager`` as a second-tier fill source.  Local hit
   short-circuits; local miss checks the HTTP cache; HTTP miss
   does the cold install and publishes back.  Backend prefix in
   the URL (``<backend>/vms/<hash>/<primary-disk-filename>``) lets
   multiple backends share one server without artifact collisions.

   Optional — opt in by passing ``cache="https://..."`` to the
   orchestrator.  Bundled in ``cache/`` as a docker-compose nginx
   stack you can run anywhere.

A single test run, end-to-end
-----------------------------

::

    1. Orchestrator(cache="https://cache.testrange").__enter__()
       ├─ Constructs StorageBackend         # transport+format from host=
       ├─ Constructs CacheManager           # persistent
       │   └─ Constructs HttpCache          # remote tier (cache= was set)
       └─ Constructs RunDir                 # fresh per-run scratch dir

    2. For each VM:
       a. CacheManager.get_image(url)
          ├─ Local hit → return path
          ├─ Local miss → HttpCache.exists/get → fill local → return
          └─ HttpCache miss → download from upstream URL
                            → store local → HttpCache.put

       b. CacheManager.get_vm(config_hash, backend)
          ├─ Local hit → return ref
          ├─ Local miss → HttpCache fill → return ref
          └─ Else: install phase runs → CacheManager.store_vm
                                      → HttpCache.put

       c. RunDir.create_overlay(vm_name, cached_disk_ref)
          └─ Backend's disk-format creates a per-run COW overlay

    3. Test runs against the VMs.

    4. Orchestrator.__exit__()
       └─ RunDir.cleanup() — deletes the entire run/<run_id>/ dir
       (CacheManager + HttpCache untouched — they persist)

Why this isn't redundant
~~~~~~~~~~~~~~~~~~~~~~~~

Each layer is answering a different question.  Collapsing any two
of them would break the others:

- **CacheManager vs RunDir** — opposite lifetimes.  Cache is
  "build once, reuse forever"; rundir is "fresh per test, gone at
  teardown."  Mixing them means every test re-installs (slow) OR
  per-run state pollutes the long-term cache (correctness disaster).
- **StorageBackend vs CacheManager/RunDir** — orthogonal axis.
  Both ask "where are my files and what tool manipulates them?"
  StorageBackend answers it once; without it both layers would
  duplicate transport logic.
- **HTTP cache vs CacheManager** — different scopes.
  CacheManager is one host's view; the HTTP cache is a fleet-shared
  tier.  CI runners, dev laptops, and CI pipelines share artifacts
  via the HTTP cache without paying download/install costs each
  time.

Backends and the abstract surface
---------------------------------

The whole package is built around the rule that **generic code only
talks to abstract bases**.  Concrete behaviour lives in
:mod:`testrange.backends`.  Every backend is a peer that ships:

- An ``Orchestrator`` subclass of
  :class:`~testrange.orchestrator_base.AbstractOrchestrator`
- A ``VM`` subclass of :class:`~testrange.vms.base.AbstractVM`
- A ``VirtualNetwork`` subclass of
  :class:`~testrange.networks.base.AbstractVirtualNetwork`
- Optionally a ``Switch`` subclass of
  :class:`~testrange.networks.base.AbstractSwitch` for backends
  that model the switch + port-group layering as a distinct
  layer (Proxmox SDN zones, future VMware vSwitches).  Backends
  whose networks are self-contained bridges (vanilla libvirt)
  can leave this unimplemented; the abstract ``switch=`` field on
  ``VirtualNetwork`` defaults to ``None`` so portable test code
  works either way.
- Optionally: backend-specific device classes (siblings of
  :class:`~testrange.HardDrive` etc. under the matching abstract
  base), pre-composed storage backends, communicators, builders.

Adding a new backend is three things:

1. ``testrange/backends/<name>/`` directory with concrete
   subclasses of every ABC.
2. Registration in
   :mod:`testrange.backends.__init__` so the CLI's URL dispatch
   knows about it.
3. An entry in ``BACKEND_TRIPLES`` in
   ``tests/test_backend_contract.py`` so the cross-backend
   scenario tests immediately exercise it — any divergence from
   the abstract contract fails loudly there.

The abstract surface is what every test author writes against.
Pinning a test to one backend is opt-in (import the backend's
concrete class explicitly) — the default
:class:`~testrange.Orchestrator` / :class:`~testrange.VM` aliases
resolve to the project's default backend (see
:doc:`/api/backends`), but nothing in your test code is forced to
know that.

The cross-backend test contract
-------------------------------

``tests/test_backend_contract.py`` runs the same scenarios against
every registered backend.  Two layers:

- **Signature contract** — every backend has the right method
  shapes (``__enter__`` / ``__exit__`` / ``cleanup`` /
  ``backend_type`` / etc.).
- **Scenario contract** — every backend honours the same
  observable behaviours (``cache.backend_name == backend_type()``;
  ``GenericVM`` gets promoted to the backend's native VM type at
  ``__init__``; ``leak()`` is idempotent; ``cleanup()`` is
  best-effort + idempotent; backend-name is deterministic for a
  given ``(spec, run_id)``).

Adding a backend to the parametrize list at the top of that file
puts it through every scenario immediately.  Any divergence is a
test failure rather than a runtime surprise.
