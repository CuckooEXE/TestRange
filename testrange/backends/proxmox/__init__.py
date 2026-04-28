"""Proxmox VE backend for TestRange (SCAFFOLDING — not yet implemented).

Importing this package succeeds without the Proxmox Python client
(``proxmoxer``) being installed.  All heavy lifting is deferred to
:meth:`ProxmoxOrchestrator.__enter__`, which raises
:class:`NotImplementedError` with a clear message explaining what still
needs to be wired up.

Once implementation lands, the package-level API mirrors the other
shipped backends:

.. code-block:: python

    from testrange.backends.proxmox import (
        ProxmoxOrchestrator,
        ProxmoxVM,
        ProxmoxVirtualNetwork,
    )

    with ProxmoxOrchestrator(host="pve.example.com", ...) as orch:
        orch.vms["web"].exec([...])

See :mod:`testrange.backends.proxmox.orchestrator` for the full TODO
list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from testrange.backends.proxmox.guest_agent import (
    ProxmoxGuestAgentCommunicator,
)
from testrange.backends.proxmox.hypervisor import ProxmoxHypervisor
from testrange.backends.proxmox.network import (
    ProxmoxSwitch,
    ProxmoxVirtualNetwork,
)
from testrange.backends.proxmox.orchestrator import ProxmoxOrchestrator
from testrange.backends.proxmox.vm import ProxmoxVM

if TYPE_CHECKING:
    from typing import Any

    from testrange.orchestrator_base import AbstractOrchestrator
    from testrange.vms.hypervisor_base import AbstractHypervisor

# ---------------------------------------------------------------------------
# CLI integration — see the corresponding section in
# :mod:`testrange.backends.libvirt` for the dispatch contract.
# ---------------------------------------------------------------------------

_CLI_URL_SCHEMES = frozenset({"proxmox"})


def cli_build_orchestrator(
    url: str, original: AbstractOrchestrator,
) -> ProxmoxOrchestrator | None:
    """Return a new :class:`ProxmoxOrchestrator` for *url*, or ``None``
    when *url* isn't a ``proxmox://`` URL."""
    from urllib.parse import parse_qs, urlparse

    import click

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _CLI_URL_SCHEMES:
        return None

    host = parsed.hostname
    if not host:
        raise click.BadParameter(
            f"proxmox orchestrator URL needs a host: {url!r}"
        )
    node = parsed.path.lstrip("/").split("/", 1)[0] or None
    qs = parse_qs(parsed.query)
    storage = qs.get("storage", [None])[0]

    # Auth resolution priority:
    #   1. ``?token=`` query param (unambiguous; allowed to include the
    #      full ``user@realm!name=secret`` string)
    #   2. userinfo without a colon → treated as a token
    #   3. userinfo with a colon → user:password
    token: str | None = qs.get("token", [None])[0]
    user: str | None = None
    password: str | None = None
    if parsed.username is not None:
        if parsed.password is not None:
            user = parsed.username
            password = parsed.password
        elif token is None:
            token = parsed.username
    if token is None and user is None:
        raise click.BadParameter(
            "proxmox orchestrator URL must include either a token "
            "(``proxmox://TOKEN@host``) or credentials "
            "(``proxmox://user:password@host``)."
        )

    return ProxmoxOrchestrator(
        host=host,
        networks=original._networks,  # type: ignore[attr-defined]
        vms=original._vm_list,  # type: ignore[attr-defined]
        cache_root=original._cache.root,  # type: ignore[attr-defined]
        node=node,
        storage=storage,
        token={"token": token, "user": user, "password": password},
    )


def hypervisor_for_orchestrator(
    orchestrator: type[AbstractOrchestrator], **kwargs: Any,
) -> AbstractHypervisor | None:
    """Return a :class:`ProxmoxHypervisor` for *orchestrator* if it's
    the proxmox backend's, else ``None``.

    Called by the top-level :func:`testrange.Hypervisor` factory's
    backend dispatcher.  Mirrors the
    :func:`cli_build_orchestrator` registry shape: each backend
    publishes one of these and returns ``None`` for orchestrator
    classes it doesn't own, letting the dispatcher try the next
    backend.
    """
    if issubclass(orchestrator, ProxmoxOrchestrator):
        return ProxmoxHypervisor(orchestrator=orchestrator, **kwargs)
    return None


__all__ = [
    "ProxmoxGuestAgentCommunicator",
    "ProxmoxHypervisor",
    "ProxmoxOrchestrator",
    "ProxmoxSwitch",
    "ProxmoxVirtualNetwork",
    "ProxmoxVM",
    "cli_build_orchestrator",
    "hypervisor_for_orchestrator",
]
