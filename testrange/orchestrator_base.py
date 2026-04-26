"""Abstract base class for hypervisor orchestrators.

TestRange separates the *what* of a test run (VM specs, networks,
builders) from the *how* (the hypervisor backend that realises those
specs).  :class:`AbstractOrchestrator` is the contract every backend
implements.

Concrete backends live under :mod:`testrange.backends`.  The default
re-exported as :class:`testrange.Orchestrator` is one of them.

The ABC lives here — separate from any specific backend — to avoid
circular imports: concrete :class:`~testrange.vms.base.AbstractVM` and
:class:`~testrange.networks.base.AbstractVirtualNetwork` subclasses
need to accept an orchestrator as a method argument, which would cycle
through a backend-specific module if the ABC lived there.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange._run import RunDir
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.storage.base import StorageBackend
    from testrange.vms.base import AbstractVM
    from testrange.vms.hypervisor_base import AbstractHypervisor


class AbstractOrchestrator(ABC):
    """Contract shared by every TestRange hypervisor backend.

    Concrete subclasses open a backend-specific control-plane
    connection, provision the supplied networks + VMs, expose them via
    :attr:`vms`, and tear everything down on exit.

    The class is a context manager.  A typical test looks like::

        with Orchestrator(networks=[...], vms=[...]) as orch:
            orch.vms["web"].exec(["uname", "-r"]).check()

    :param host: Backend-specific target string.  Each backend
        documents its own accepted shapes (URI / hostname / cluster
        entry point); the local-host default is ``"localhost"``.
    :param networks: Virtual networks to create for this run.
    :param vms: Virtual machines to provision and start.
    :param cache_root: Override the default cache directory.
    """

    vms: dict[str, AbstractVM]
    """Running VMs keyed by name.  Populated by :meth:`__enter__`.

    Callers are expected to treat this as read-only; backends mutate
    it via their own internal code paths.  We declare ``dict`` rather
    than :class:`~collections.abc.Mapping` because Pyright's strict
    override rule requires identical types for mutable members —
    using ``Mapping`` here would force concrete backends to use
    ``Mapping`` too, which prevents their own internal ``__setitem__``
    calls.  ``dict[str, AbstractVM]`` on both sides satisfies both
    concerns: the ABC type matches concrete types exactly, and
    concrete code can populate the dict normally.
    """

    # ------------------------------------------------------------------
    # Protected state shared by all backends.  Declared here (not just
    # on concrete subclasses) so cross-backend code — ``testrange._cli``
    # reaching into an arbitrary orchestrator's configured networks
    # when swapping backends, ``testrange._repl`` inspecting the run
    # dir — type-checks cleanly.  Leading underscore signals "internal
    # to backend implementations" but still publicly typed.
    # ------------------------------------------------------------------

    _vm_list: list[AbstractVM]
    """The VM specs this orchestrator was constructed with."""

    _networks: list[AbstractVirtualNetwork]
    """The network specs this orchestrator was constructed with."""

    _run: RunDir | None
    """Scratch directory for the current run; ``None`` outside one."""

    _leaked: bool = False
    """Set by :meth:`leak` to tell :meth:`__exit__` to skip resource
    teardown.  The backend connection itself still closes — just the
    VMs, networks, and run scratch directory survive."""

    @classmethod
    @abstractmethod
    def backend_type(cls) -> str:
        """Short identifier for this backend.

        Test authors can branch on this when a check has to be
        backend-specific — e.g. skipping an assertion that doesn't
        apply on a particular hypervisor::

            if orchestrator.backend_type() == "some-backend":
                assert orchestrator.vms["web"].exec(["ls", "/dev/vda"]).exit_code == 0

        Returned as a class attribute so callers can reason about the
        backend without instantiating the orchestrator.
        """

    def __init__(
        self,
        host: str = "localhost",
        networks: Sequence[AbstractVirtualNetwork] | None = None,
        vms: Sequence[AbstractVM] | None = None,
        cache_root: Path | None = None,
        cache: str | None = None,
        cache_verify: bool | str = True,
        storage_backend: StorageBackend | None = None,
    ) -> None:
        """Store inputs — subclasses override to open the backend
        connection and initialise handles.

        :param cache: Optional URL of an :doc:`HTTP cache </usage/http_cache>`
            (e.g. ``"https://cache.testrange"``) consulted as a
            second-tier fill source for downloaded base images and
            post-install VM snapshots.  ``None`` (default) uses only
            the local on-disk cache.
        :param cache_verify: TLS verification for the HTTP cache.
            ``True`` (default) requires a trusted cert chain;
            ``False`` accepts self-signed (matches the bundled
            ``cache/`` docker setup); a string is treated as a path
            to a CA bundle.  Ignored when *cache* is ``None``.
        :param storage_backend: Optional explicit
            :class:`~testrange.storage.StorageBackend` (transport +
            disk format) for this run.  When ``None`` (default) each
            backend infers a sensible default from *host* and its
            own URL conventions — the libvirt backend picks
            :class:`~testrange.storage.LocalStorageBackend` for
            local URIs and :class:`~testrange.storage.SSHStorageBackend`
            for ``qemu+ssh://``; other backends document their own
            inference in their concrete subclass docstring.  Pass
            explicitly when the auto-selection logic can't guess
            (custom remote paths, a fake backend in tests, an
            uncommon transport+format pairing).
        """
        del host, networks, vms, cache_root, cache, cache_verify  # subclasses wire these
        del storage_backend

    @abstractmethod
    def __enter__(self) -> AbstractOrchestrator:
        """Open the backend connection, provision networks + VMs, and
        populate :attr:`vms`.

        :returns: ``self``, ready for the test function to use.
        """

    @abstractmethod
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Tear down every VM, network, and backend handle.

        Must never raise — teardown failures are logged but swallowed
        so they cannot mask the original exception (if any) that ended
        the ``with`` block.
        """

    def cleanup(self, run_id: str) -> None:
        """Tear down resources from a prior run that exited uncleanly.

        Reconstructs the deterministic backend names this orchestrator
        would have created for *run_id* — which is the only
        nondeterministic input — and tries to destroy each.  Used
        from the CLI as ``testrange cleanup MODULE[:FACTORY] RUN_ID``
        when a previous run was killed before its ``__exit__`` could
        run (``kill -9``, host reboot, OOM, etc.).

        Does NOT call :meth:`__enter__` — there's nothing to
        provision, just orphaned resources to delete.  Implementations
        open whatever connection they need, enumerate the names the
        spec + run_id imply, and best-effort delete each.  Already-
        deleted resources are silently skipped so cleanup is
        idempotent.

        :param run_id: UUID4 string from the original run, the only
            nondeterministic input.  Find it in the run's log output
            (``run id <uuid>``) or in
            ``<cache_root>/runs/<run_id>/``.
        :raises NotImplementedError: When this backend hasn't wired
            cleanup yet.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement cleanup() yet — "
            "the backend cannot tear down a leaked run.  Until that "
            "lands, clean up by hand using its native tools."
        )

    def keep_alive_hints(self) -> list[str]:
        """Return shell commands a user would run to clean up resources
        left behind by ``testrange repl --keep`` or :meth:`leak`.

        Each entry is a self-contained shell line (no chaining needed
        by the caller).  The default returns an empty list — backends
        that can meaningfully advise on manual cleanup (virsh destroy,
        ``qm destroy``, REST DELETE via curl, …) override this.
        """
        return []

    def leak(self) -> None:
        """Mark this run so :meth:`__exit__` does not tear resources down.

        Intended for scripting TestRange as a thin VM-provisioner
        rather than a test harness — when you want the ``with``
        block's provisioning guarantees but don't want the VMs
        destroyed when the block ends::

            with Orchestrator(networks=[...], vms=[...]) as orch:
                vm = orch.vms["box"]
                vm.exec(["apt-get", "install", "-y", "my-tool"]).check()
                orch.leak()
            # ``vm`` is still running.  Connect to it over SSH,
            # hand it to another tool, snapshot it, whatever — TestRange
            # is done with it.

        The backend control-plane connection itself still closes
        normally; only the provisioned **resources** (VMs, networks,
        install-phase run directory) are preserved.  On exit the
        orchestrator logs the commands a human would run to clean up
        — same list :meth:`keep_alive_hints` produces for
        ``testrange repl --keep``.

        **Footguns to know about:**

        - **Disk leak:** the run directory at
          ``<cache_root>/runs/<run_id>/`` stays on disk indefinitely
          (it holds the leaked VMs' per-run scratch — overlays,
          firmware-state files, seed ISOs, etc.).  The log line on
          exit includes its path so you can ``rm -rf`` when you're
          done.
        - **Install-subnet pool pressure:** each leaked run holds one
          of the 16 install-phase subnets from the 192.168.240-254/24
          pool until the associated network is destroyed.  Enough
          leaks and future :meth:`__enter__` calls run out of install
          subnets.
        - **Memory pressure:** leaked VMs continue to consume host
          RAM.  The preflight check on future runs is computed
          against live ``/proc/meminfo``, so it'll correctly account
          for them — you'll just see fewer VMs fit per host.
        - **Reversibility:** ``leak()`` is one-way for the current
          context-manager scope.  There is no ``unleak()``; if you
          decide you want a normal teardown, don't call ``leak()`` in
          the first place.
        - **Idempotent:** calling ``leak()`` twice is a no-op.

        Every backend honours the flag the same way as long as its
        ``__exit__`` / ``_teardown`` short-circuits on
        ``self._leaked``.  The base class sets the bit; backends
        check it.
        """
        self._leaked = True

    @classmethod
    def root_on_vm(
        cls,
        hypervisor: AbstractHypervisor,
        outer: AbstractOrchestrator,
    ) -> AbstractOrchestrator:
        """Return a fresh orchestrator whose control plane lives
        *inside* ``hypervisor``.

        Called by the outer orchestrator's :meth:`__enter__` once
        ``hypervisor`` is booted and its communicator is reachable.
        The returned orchestrator is **not yet entered** — the caller
        does that via ``ExitStack``.

        Each driver builds the inner control plane in its own
        backend-native way (typically: bring up the inner control-
        plane endpoint, authenticate against it with credentials
        seeded into the hypervisor at install time, return a
        configured orchestrator pointing at it).

        The *outer* orchestrator passes itself as ``outer`` so the
        inner driver can reuse shared state (cache root, storage
        backend factories) without guessing.

        :param hypervisor: The just-booted hypervisor VM.  Its
            :attr:`~AbstractHypervisor.vms` /
            :attr:`~AbstractHypervisor.networks` become the inner
            orchestrator's ``vms`` / ``networks`` inputs.
        :param outer: The outer orchestrator that booted
            ``hypervisor``.  Used to source the shared cache root.
        :returns: A configured (not yet entered) orchestrator
            instance.
        :raises NotImplementedError: If this driver does not yet
            support being rooted on a VM.  Drivers that haven't
            implemented nested orchestration must leave the default
            implementation in place.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not support nested orchestration "
            "yet — it cannot be rooted on a hypervisor VM.  Use a "
            "backend whose orchestrator implements root_on_vm()."
        )


__all__ = ["AbstractOrchestrator"]
