Extending TestRange
===================

TestRange's concrete classes sit on top of thin abstract base
classes.  Each concern — VM, network, communicator, package —
has its own ABC so you can add new backends without touching the
orchestrator.

New package managers
--------------------

Subclass :class:`~testrange.packages.AbstractPackage`.  At a minimum
you implement:

- ``package_manager`` property — a short identifier (e.g. ``"nix"``).
  The cloud-init builder uses this to decide whether to emit the
  package in the native ``packages:`` list or via ``runcmd``.
- ``native_package_name()`` — for managers cloud-init handles
  natively, return the package name string; otherwise return
  ``None`` and populate ``install_commands()``.
- ``install_commands()`` — list of shell commands to run under
  ``runcmd`` to install the package.

Example:

.. code-block:: python

    from testrange.packages import AbstractPackage

    class Nix(AbstractPackage):
        def __init__(self, attr: str) -> None:
            self.attr = attr

        @property
        def package_manager(self) -> str:
            return "nix"

        def native_package_name(self) -> str | None:
            return None  # not cloud-init-native; use runcmd

        def install_commands(self) -> list[str]:
            return [f"nix-env -iA nixpkgs.{self.attr}"]

        def __repr__(self) -> str:
            return f"Nix({self.attr!r})"

That's enough for :class:`~testrange.vms.cloud_init.CloudInitBuilder`
to pick it up on the ``runcmd`` path.  The ``__repr__`` is important:
it feeds :func:`~testrange.cache.vm_config_hash`, so two ``Nix("jq")``
packages are cache-equivalent and a ``Nix("ripgrep")`` is not.

New communicators
-----------------

Subclass :class:`~testrange.communication.base.AbstractCommunicator`
and implement ``exec``, ``get_file``, ``put_file``, and
``hostname``.  The VM class calls these through
:meth:`~testrange.vms.base.AbstractVM._require_communicator`; the
file helpers (``read_text`` / ``write_text`` / ``upload`` /
``download``) delegate down to your ``get_file`` / ``put_file`` so
you don't need to reimplement them.

If your protocol needs a readiness handshake (equivalent to
``wait_ready`` for the guest agent), expose it as a method and call
it from whichever VM subclass instantiates the communicator.

New VM backends
---------------

Subclass :class:`~testrange.vms.base.AbstractVM` if you're
integrating with something other than libvirt — say VirtualBox, or
a cloud provider's VM API.  You need to:

1. Implement a ``name`` property returning the VM's human name.
2. Implement ``shutdown()``.
3. Arrange for ``self._communicator`` to be populated with a live
   :class:`~testrange.communication.base.AbstractCommunicator` by the
   time test code calls methods on the VM.

The orchestrator doesn't assume libvirt — it calls ``vm.build(...)``
and ``vm.start_run(...)`` if those exist, but the AbstractVM
interface itself is just the runtime surface (``exec``, ``hostname``,
file helpers, ``shutdown``).  For a non-libvirt backend you'd write
your own driver logic that produces running VMs with a populated
communicator, then pass those VMs to whatever subset of the
orchestrator makes sense.

New virtual networks
--------------------

Subclass :class:`~testrange.networks.base.AbstractVirtualNetwork`
and implement ``start`` / ``stop`` / ``bind_run`` / ``register_vm``
/ ``backend_name``.  The network-XML generation in the libvirt
subclass is a reasonable reference for what those calls need to
produce.

Design principles
-----------------

A few norms the existing modules follow — worth matching if you're
upstreaming your extension:

1. **Runtime state is nullable until provisioned.**  Fields that only
   make sense after ``bind_run`` / ``start_run`` / equivalent are
   typed ``T | None`` and set to ``None`` at construction.  Callers
   get a clear error (via ``_require_communicator`` or an
   ``assert``/``raise`` in the method) rather than an ``AttributeError``.

2. **Teardown never raises.**  Cleanup code in ``__exit__`` /
   ``stop()`` / ``_teardown`` wraps every step so a failure in one
   doesn't mask the original exception.  If you want to surface
   cleanup problems, log them at ``WARNING``; don't re-raise.

3. **Hash inputs are explicit.**  If you add a field that affects
   which image gets built, fold it into
   :func:`~testrange.cache.vm_config_hash`.  If you add a field that
   shouldn't (like SSH keys or runtime IPs), leave it out.

4. **Log, don't print.**  Every long-running operation brackets
   itself in :func:`testrange._logging.log_duration` so users can see
   progress.  New backends should do the same.
