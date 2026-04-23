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
    from testrange.vms.base import AbstractVM


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
    ) -> None:
        """Store inputs — subclasses override to open the backend
        connection and initialise handles.
        """
        del host, networks, vms, cache_root  # subclasses wire these

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

    def keep_alive_hints(self) -> list[str]:
        """Return shell commands a user would run to clean up resources
        left behind by ``testrange repl --keep``.

        Each entry is a self-contained shell line (no chaining needed
        by the caller).  The default returns an empty list — backends
        that can meaningfully advise on manual cleanup (virsh destroy,
        ``qm destroy``, REST DELETE via curl, …) override this.

        Called only by the ``--keep`` path of the REPL; never in the
        normal teardown flow.
        """
        return []


__all__ = ["AbstractOrchestrator"]
