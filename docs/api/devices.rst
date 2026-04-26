Devices
=======

A VM's hardware shape is described by a flat list of device objects
on its ``devices=`` keyword.  Each object is a plain Python class
with a few fields; the backend inspects the list with ``isinstance``
checks at build time to render the hypervisor-native domain spec
(libvirt domain XML, Proxmox REST payload, …) and the builder
network-config for the guest.

Generic vs backend-specific
---------------------------

Each device kind ships in two flavours:

* A **generic** version (:class:`vCPU`, :class:`Memory`,
  :class:`HardDrive`, :class:`vNIC`) that every backend
  accepts.  Carries only the universal fields; backends pick
  sensible defaults for everything else.  Reach for these unless
  you need a backend-specific knob.
* **Backend-specific** subclasses (e.g.
  :class:`testrange.backends.libvirt.LibvirtHardDrive` with bus
  selection and the NVMe shortcut) that expose extra options
  meaningful only to that backend.  These are **siblings** of the
  generic class, not children — that's the type-system contract
  that makes pyright reject a libvirt-specific drive being passed
  to a Proxmox VM.

Each backend's ``VM`` class declares a typed union of accepted
device classes; passing a foreign-backend device is caught by the
type checker at edit time and again at runtime in ``__init__``.

.. code-block:: python

    from testrange import VM, HardDrive, vCPU, Memory, vNIC
    from testrange.backends.libvirt import LibvirtHardDrive

    VM(
        name="db",
        iso="debian-12",
        users=[Credential("root", "pw")],
        devices=[
            vCPU(4),
            Memory(8),
            LibvirtHardDrive(200, nvme=True),  # libvirt-specific knobs
            HardDrive(500),                    # generic — backend picks bus
            vNIC("Internal"),
            vNIC("Public", ip="10.0.0.5"),
        ],
    )

Defaults
--------

Omitting a device type yields a sensible default:

- **vCPU**: 2 cores.
- **Memory**: 2 GiB.
- **HardDrive**: 20 GB primary disk (backend-default bus —
  virtio-blk on libvirt for Linux guests, SATA for Windows).
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

Backend-specific
~~~~~~~~~~~~~~~~

.. autoclass:: testrange.backends.libvirt.LibvirtHardDrive
   :members:
   :show-inheritance:

.. autofunction:: testrange.devices.parse_size

.. autofunction:: testrange.devices.normalise_size
