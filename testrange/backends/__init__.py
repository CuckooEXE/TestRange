"""Hypervisor backends for TestRange.

Each subpackage implements the abstract contracts in
:mod:`testrange.orchestrator_base`, :mod:`testrange.vms.base`, and
:mod:`testrange.networks.base`.  Third-party code plugging in a new
backend does so by mirroring the same layout.

Shipped backends
----------------

- :mod:`testrange.backends.libvirt` — KVM / QEMU via libvirt.  Default
  for the top-level re-exports (:class:`testrange.Orchestrator` etc.).
- :mod:`testrange.backends.proxmox` — Proxmox VE scaffolding.  Not yet
  implemented; importing succeeds but instantiation raises
  :class:`NotImplementedError`.

CLI URL dispatch
----------------

Each backend's ``__init__`` exposes a ``cli_build_orchestrator(url,
original)`` function that returns an orchestrator when *url* is one
that backend handles, or ``None`` otherwise.  The ``testrange``
command's ``--orchestrator URL`` flag iterates over
:data:`_CLI_BACKENDS` and uses the first match, so adding a new
backend is an additive change — the CLI module learns nothing
backend-specific.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from testrange.exceptions import OrchestratorError

if TYPE_CHECKING:
    from typing import Any

    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.vms.hypervisor_base import AbstractHypervisor


_CLI_BACKENDS: tuple[str, ...] = (
    "testrange.backends.libvirt",
    "testrange.backends.proxmox",
)
"""Module names of backends participating in ``--orchestrator URL`` dispatch.

Iterated in order; the first backend whose ``cli_build_orchestrator``
returns non-``None`` wins.  Third parties can append to this tuple at
import time (or register via their own shim).
"""


def cli_build_orchestrator(
    url: str,
    original: AbstractOrchestrator,
) -> AbstractOrchestrator | None:
    """Dispatch *url* to the first registered backend that claims it.

    :param url: User-supplied ``--orchestrator URL`` value.
    :param original: The test's original orchestrator — passed through
        so the matched backend can reuse its ``vms`` / ``networks`` /
        cache root.
    :returns: A new orchestrator on match, or ``None`` when no backend
        claims *url*.
    """
    for mod_name in _CLI_BACKENDS:
        mod = importlib.import_module(mod_name)
        builder = getattr(mod, "cli_build_orchestrator", None)
        if builder is None:
            continue
        result = builder(url, original)
        if result is not None:
            return result
    return None


def hypervisor_for_orchestrator(
    orchestrator: type[AbstractOrchestrator],
    **kwargs: Any,
) -> AbstractHypervisor:
    """Construct the hypervisor class native to *orchestrator*.

    Each backend exposes a ``hypervisor_for_orchestrator(cls, **kwargs)``
    function that returns its own concrete :class:`AbstractHypervisor`
    subclass when *cls* is one of its orchestrators, or ``None``
    otherwise.  We iterate the registry in the same order
    :func:`cli_build_orchestrator` does and return the first match.

    Used by the top-level :func:`testrange.Hypervisor` factory; see
    its docstring for the call shape.

    :raises OrchestratorError: If no registered backend handles
        *orchestrator*.
    """
    for mod_name in _CLI_BACKENDS:
        mod = importlib.import_module(mod_name)
        builder = getattr(mod, "hypervisor_for_orchestrator", None)
        if builder is None:
            continue
        result = builder(orchestrator, **kwargs)
        if result is not None:
            return result
    raise OrchestratorError(
        f"No hypervisor implementation registered for orchestrator "
        f"class {orchestrator.__name__!r}.  Either pass an orchestrator "
        f"class from one of the shipped backends "
        f"(testrange.backends.libvirt.Orchestrator, "
        f"testrange.backends.proxmox.ProxmoxOrchestrator), or add the "
        f"backend's package to testrange.backends._CLI_BACKENDS."
    )


__all__ = ["cli_build_orchestrator", "hypervisor_for_orchestrator"]
