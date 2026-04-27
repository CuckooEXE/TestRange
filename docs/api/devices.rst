Devices
=======

A VM's hardware shape is described by a flat list of device objects
on its ``devices=`` keyword.  Each object is a plain Python class
with a few fields; the backend inspects the list with ``isinstance``
checks at build time to render the hypervisor-native domain spec
and the builder network-config for the guest.

Generic vs backend-specific
---------------------------

Each device kind ships in two flavours:

* A **generic** version (:class:`vCPU`, :class:`Memory`,
  :class:`HardDrive`, :class:`vNIC`) that every backend
  accepts.  Carries only the universal fields; backends pick
  sensible defaults for everything else.  Reach for these unless
  you need a backend-specific knob.
* **Backend-specific** subclasses (each backend's
  ``<Backend>HardDrive`` etc. under ``testrange.backends.<backend>``)
  that expose extra options meaningful only to that backend.  These
  are **siblings** of the generic class, not children — that's the
  type-system contract that makes pyright reject one backend's
  device being passed to another backend's VM.

Each backend's ``VM`` class declares a typed union of accepted
device classes; passing a foreign-backend device is caught by the
type checker at edit time and again at runtime in ``__init__``.

.. code-block:: python

    from testrange import VM, HardDrive, vCPU, Memory, vNIC
    # Backend-specific drive comes from your chosen backend's module:
    from testrange.backends.<backend> import <Backend>HardDrive

    VM(
        name="db",
        iso="debian-12",
        users=[Credential("root", "pw")],
        devices=[
            vCPU(4),
            Memory(8),
            <Backend>HardDrive(200, ...),     # backend-specific knobs
            HardDrive(500),                   # generic — backend picks bus
            vNIC("Internal"),
            vNIC("Public", ip="10.0.0.5"),
        ],
    )

Defaults
--------

Omitting a device type yields a sensible default:

- **vCPU**: 2 cores.
- **Memory**: 2 GiB.
- **HardDrive**: 20 GB primary disk (backend picks the default bus
  — typically virtio-blk for Linux guests, SATA for Windows).
- **Network refs**: none (a VM with no NICs still boots; useful for
  compute-only scenarios).

If you pass multiple ``HardDrive`` entries, **the first becomes the
OS disk** — cloud-init installs onto it and the post-install
snapshot lands in the cache keyed off its size.  Subsequent entries
are empty data volumes attached as ``vdb``, ``vdc``, ...; they live
in the per-run scratch dir and are discarded at teardown.  vCPU and
Memory are single-valued — only the first instance in the list is
used.

Sizes
-----

``HardDrive`` accepts either:

- **A number** (``int`` or ``float``), interpreted as GiB —
  ``HardDrive(32)`` is 32 GiB, ``HardDrive(1.5)`` is 1.5 GiB.  This
  is the ergonomic default for the common case of whole-GiB sizing.
- **A size string** — ``"64GB"``, ``"512M"``, ``"1T"``, etc.  The
  parser accepts any of ``B``, ``K``/``KB``/``KiB``,
  ``M``/``MB``/``MiB``, ``G``/``GB``/``GiB``, ``T``/``TB``/``TiB``
  (case-insensitive) and is deliberately lenient with binary vs.
  decimal units because almost no one is precise about it.

Reference
---------

.. autoclass:: testrange.devices.vCPU
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.Memory
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.HardDrive
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.vNIC
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.AbstractDevice
   :members:
   :show-inheritance:

Sealed bases
~~~~~~~~~~~~

Backend-specific device subclasses extend these, not the concrete
generic classes — that's how the type system catches a backend's
device being passed to a different backend's VM.

.. autoclass:: testrange.devices.AbstractHardDrive
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.AbstractVCPU
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.AbstractMemory
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.AbstractVNIC
   :members:
   :show-inheritance:

Backend-specific concrete device classes are documented in their
own backend module — see :doc:`backends`.

.. autofunction:: testrange.devices.parse_size

.. autofunction:: testrange.devices.normalise_size
