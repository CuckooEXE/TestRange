"""Driver registry — maps Hypervisor types, driver names, and schemes to factories.

Concrete drivers register themselves at module import time. Three driver
dispatch paths share this one registry, plus the pin introspection helpers
the binding resolver (CORE-10) uses; no other module knows that, e.g.,
``MockHypervisor`` maps to ``MockDriver``:

- by **Hypervisor data type** from a *concrete* Plan entry (``MockHypervisor``
  -> ``MockDriver``) — the orchestrator's pinned entry point (``driver_for``);
- by **driver class name** recorded in state.json (``"MockDriver"``) — the
  cleanup entry point (``driver_for_name``);
- plus ``scheme_for_hypervisor`` and ``is_pinned`` for the binding resolver.

The ``--connect`` profile path is **not** in this registry: each concrete
:class:`~testrange.connect.BackendProfile` subclass builds its own driver
(``profile.build_driver()``) and self-registers under
:data:`testrange.connect._PROFILE_BY_SCHEME`. ``scheme`` is still kept here
because the binding resolver needs to compare a pinned entry's scheme against a
profile's scheme without going through a profile instance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError

_FROM_HYP: dict[type, Callable[[Any], HypervisorDriver]] = {}
_FROM_NAME: dict[str, Callable[[str], HypervisorDriver]] = {}
_SCHEME_FOR_HYP: dict[type, str] = {}


def register(
    *,
    hypervisor_cls: type,
    driver_name: str,
    scheme: str,
    from_hypervisor: Callable[[Any], HypervisorDriver],
    from_uri: Callable[[str], HypervisorDriver],
) -> None:
    """Register a driver's construction paths and its short scheme token.

    ``from_hypervisor`` builds the driver from a concrete Plan-time Hypervisor
    data type (the pinned orchestrator entry point). ``from_uri`` rebuilds it
    from the connection URI stored in state.json (the cleanup entry point).
    ``scheme`` is the short token a profile names the driver by (``"mock"``,
    ``"proxmox"``, ``"libvirt"``); the ``--connect`` path uses it to dispatch
    to the matching :class:`~testrange.connect.BackendProfile` subclass.
    """
    _FROM_HYP[hypervisor_cls] = from_hypervisor
    _FROM_NAME[driver_name] = from_uri
    _SCHEME_FOR_HYP[hypervisor_cls] = scheme


def driver_for(hypervisor: Any) -> HypervisorDriver:
    """Construct the driver registered for ``type(hypervisor)``."""
    factory = _FROM_HYP.get(type(hypervisor))
    if factory is None:
        raise DriverError(
            f"no driver registered for hypervisor type "
            f"{type(hypervisor).__name__}; registered: "
            f"{sorted(c.__name__ for c in _FROM_HYP)}"
        )
    return factory(hypervisor)


def driver_for_name(driver_name: str, uri: str) -> HypervisorDriver:
    """Construct the driver registered under ``driver_name`` from a URI."""
    factory = _FROM_NAME.get(driver_name)
    if factory is None:
        raise DriverError(
            f"no driver registered under name {driver_name!r}; registered: {sorted(_FROM_NAME)}"
        )
    return factory(uri)


def scheme_for_hypervisor(hypervisor: Any) -> str | None:
    """The scheme of a *concrete* Hypervisor entry, or ``None`` if it is generic.

    ``None`` means the entry's type is unregistered — the backend-agnostic
    :class:`~testrange.hypervisor.Hypervisor` — so it pins no driver and the
    binding must come from a connection profile (CORE-10).
    """
    return _SCHEME_FOR_HYP.get(type(hypervisor))


def is_pinned(hypervisor: Any) -> bool:
    """True if this Plan entry pins a concrete driver (its type is registered)."""
    return type(hypervisor) in _FROM_HYP
