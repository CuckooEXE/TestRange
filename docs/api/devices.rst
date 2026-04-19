Devices
=======

A VM's hardware shape is described by a flat list of device objects
on its ``devices=`` keyword.  Each object is a plain Python class
with a few fields; the backend inspects the list with ``isinstance``
checks at build time to render the hypervisor-native domain spec
(libvirt domain XML, Proxmox REST payload, …) and the builder
network-config for the guest.

The pattern is intentionally flat and composable rather than a
single "spec" dict:

.. code-block:: python

    VM(
        name="db",
        iso="debian-12",
        users=[Credential("root", "pw")],
        devices=[
            vCPU(4),
            Memory(8),
            HardDrive(200, nvme=True),       # 200 GiB OS disk (NVMe)
            HardDrive(500),                  # 500 GiB data disk
            VirtualNetworkRef("Internal"),
            VirtualNetworkRef("Public", ip="10.0.0.5"),
        ],
    )

Defaults
--------

Omitting a device type yields a sensible default:

- **vCPU**: 2 cores.
- **Memory**: 2 GiB.
- **HardDrive**: 20 GB primary disk (virtio-blk).
- **Network refs**: none (a VM with no NICs still boots; useful for
  compute-only scenarios).

If you pass multiple ``HardDrive`` entries, **the first becomes the
OS disk** — cloud-init installs onto it and the post-install
snapshot lands in the cache keyed off its size.  Subsequent entries
are empty data volumes attached as ``vdb``, ``vdc``, ... (or
``nvme1n1``, ``nvme2n1``, ... if ``nvme=True``); they live in the
per-run scratch dir and are discarded at teardown.  vCPU and Memory
are single-valued — only the first instance in the list is used.

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

.. autoclass:: testrange.devices.VirtualNetworkRef
   :members:
   :show-inheritance:

.. autoclass:: testrange.devices.AbstractDevice
   :members:
   :show-inheritance:

.. autofunction:: testrange.devices.parse_size

.. autofunction:: testrange.devices.normalise_qemu_size
