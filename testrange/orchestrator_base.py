"""Abstract base class for hypervisor orchestrators.

TestRange separates the *what* of a test run (VM specs, networks,
builders) from the *how* (the hypervisor backend that realises those
specs).  :class:`AbstractOrchestrator` is the contract every backend
implements.

Concrete backends live in sibling modules:

- :mod:`testrange.orchestrator` — the default libvirt / KVM / QEMU
  backend.  Exported as :class:`~testrange.Orchestrator` at the
  package top level; the explicit name is
  :class:`~testrange.orchestrator.LibvirtOrchestrator`.
- :mod:`testrange.backends.proxmox` — Proxmox VE scaffolding.  Not yet
  implemented.

The ABC lives in a separate module from the libvirt orchestrator to
avoid circular imports — the libvirt :class:`VM` and
:class:`VirtualNetwork` implementations need to accept the
orchestrator as a method argument (see their tightened abstract
signatures), which would cycle through ``testrange.orchestrator`` if
the ABC lived there.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testrange.networks.base import AbstractVirtualNetwork
    from testrange.vms.base import AbstractVM


class AbstractOrchestrator(ABC):
    """Contract shared by every TestRange hypervisor backend.

    Concrete subclasses open a backend-specific connection
    (``libvirt.open(uri)``, a Proxmox REST client, …), provision
    the supplied networks + VMs, expose them via :attr:`vms`, and
    tear everything down on exit.

    The class is a context manager.  A typical test looks like::

        with Orchestrator(networks=[...], vms=[...]) as orch:
            orch.vms["web"].exec(["uname", "-r"]).check()

    :param host: Backend-specific target string.  For libvirt, a full
        URI (``qemu:///system``, ``qemu+ssh://user@host/system``) or a
        shorthand hostname; for Proxmox, a cluster entry point.
        Defaults to ``"localhost"``.
    :param networks: Virtual networks to create for this run.
    :param vms: Virtual machines to provision and start.
    :param cache_root: Override the default cache directory.
    """

    vms: dict[str, AbstractVM]
    """Running VMs keyed by name.  Populated by :meth:`__enter__`."""

    @classmethod
    @abstractmethod
    def backend_type(cls) -> str:
        """Short identifier for this backend (e.g. ``"libvirt"``,
        ``"proxmox"``).

        Test authors can branch on this when a check has to be
        backend-specific — e.g. skipping an assertion that doesn't
        apply on a particular hypervisor::

            if orchestrator.backend_type() == "libvirt":
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


__all__ = ["AbstractOrchestrator"]
