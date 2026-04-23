"""libvirt / KVM / QEMU backend for TestRange.

This is backend zero — the default that the top-level package
symbols (:class:`testrange.Orchestrator`, :class:`testrange.VM`,
:class:`testrange.VirtualNetwork`) resolve to.  See
:doc:`/api/backends` for the abstract contracts every backend
satisfies and for the status of other backends.

Direct imports:

.. code-block:: python

    from testrange.backends.libvirt import (
        Orchestrator,
        VM,
        VirtualNetwork,
        GuestAgentCommunicator,
    )

are functionally identical to the top-level
:class:`testrange.Orchestrator` / :class:`testrange.VM` /
:class:`testrange.VirtualNetwork` — the top-level names are thin
re-exports of the names defined in this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.libvirt.guest_agent import GuestAgentCommunicator
from testrange.backends.libvirt.network import VirtualNetwork
from testrange.backends.libvirt.orchestrator import (
    LibvirtOrchestrator,
    Orchestrator,
)
from testrange.backends.libvirt.vm import VM

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator

# ---------------------------------------------------------------------------
# CLI integration — consumed by ``testrange --orchestrator URL``.  Each
# backend self-describes which URL schemes it handles and how to build
# an orchestrator from one; the dispatcher in :mod:`testrange._cli`
# iterates over registered backends and uses the first that claims the
# URL.  Keeping the parsing here (rather than centralised in the CLI)
# means adding a new backend is an additive change — the CLI learns
# nothing backend-specific.
# ---------------------------------------------------------------------------

_CLI_URL_SCHEMES = frozenset({"qemu", "qemu+ssh", "qemu+tcp", "qemu+tls", "libvirt"})
"""URL schemes that resolve to this backend.

``qemu[...]://`` schemes pass straight through to libvirt's own URI
parser; ``libvirt://[user@]host`` is a convenience alias we rewrite to
``qemu+ssh://[user@]host/system`` (libvirt itself doesn't define a
``libvirt://`` scheme)."""


def cli_build_orchestrator(
    url: str, original: AbstractOrchestrator,
) -> Orchestrator | None:
    """Return a new libvirt :class:`Orchestrator` for *url*, or ``None``.

    Called by the CLI's URL dispatcher.  *original* is the
    orchestrator the test author constructed; its ``vms`` and
    ``networks`` are reused on the new backend.  Returning ``None``
    tells the dispatcher to try the next backend.
    """
    from urllib.parse import urlparse

    scheme = urlparse(url).scheme.lower()
    if scheme not in _CLI_URL_SCHEMES:
        return None

    # ``libvirt://[user@]host`` → ``qemu+ssh://[user@]host/system``.
    host = (
        "qemu+ssh://" + url[len("libvirt://"):] + "/system"
        if scheme == "libvirt"
        else url
    )
    return Orchestrator(
        host=host,
        networks=original._networks,  # type: ignore[attr-defined]
        vms=original._vm_list,  # type: ignore[attr-defined]
        cache_root=original._cache.root,  # type: ignore[attr-defined]
    )


__all__ = [
    "GuestAgentCommunicator",
    "VirtualNetwork",
    "Orchestrator",
    "LibvirtOrchestrator",
    "VM",
    "cli_build_orchestrator",
]
