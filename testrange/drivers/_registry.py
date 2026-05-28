"""Driver registry — maps Hypervisor types, driver names, and schemes to factories.

Concrete drivers register themselves at module import time. Four dispatch paths
share this one registry; no other module knows that, e.g., ``MockHypervisor``
maps to ``MockDriver`` or that the ``"mock"`` scheme builds one:

- by **Hypervisor data type** from a *concrete* Plan entry (``MockHypervisor``
  -> ``MockDriver``) — the orchestrator's pinned entry point (``driver_for``);
- by **driver class name** recorded in state.json (``"MockDriver"``) — the
  cleanup entry point (``driver_for_name``);
- by **connection-profile scheme** (``profile["driver"] == "mock"``) — the
  ``--connect`` entry point for a *generic* Plan (``driver_for_profile``);
- plus two introspection helpers the binding resolver (CORE-10) uses to tell a
  pinned (concrete) Plan entry from a generic one: ``scheme_for_hypervisor`` and
  ``is_pinned``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError

_FROM_HYP: dict[type, Callable[[Any], HypervisorDriver]] = {}
_FROM_NAME: dict[str, Callable[[str], HypervisorDriver]] = {}
_BY_SCHEME: dict[str, Callable[[Mapping[str, Any]], HypervisorDriver]] = {}
_SCHEME_FOR_HYP: dict[type, str] = {}


def register(
    *,
    hypervisor_cls: type,
    driver_name: str,
    scheme: str,
    from_hypervisor: Callable[[Any], HypervisorDriver],
    from_uri: Callable[[str], HypervisorDriver],
    from_profile: Callable[[Mapping[str, Any]], HypervisorDriver],
) -> None:
    """Register a driver's construction paths and its short scheme token.

    ``from_hypervisor`` builds the driver from a concrete Plan-time Hypervisor
    data type (the pinned orchestrator entry point). ``from_uri`` rebuilds it
    from the connection URI stored in state.json (the cleanup entry point).
    ``from_profile`` builds it from a connection-profile mapping (the
    ``--connect`` entry point). ``scheme`` is the short token a profile names
    the driver by (``"mock"``, ``"proxmox"``, ``"libvirt"``).
    """
    _FROM_HYP[hypervisor_cls] = from_hypervisor
    _FROM_NAME[driver_name] = from_uri
    _BY_SCHEME[scheme] = from_profile
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


def driver_for_profile(profile: Mapping[str, Any]) -> HypervisorDriver:
    """Construct the driver named by ``profile["driver"]`` from the profile mapping.

    The mapping is the connection profile's connection fields (CORE-9
    ``BackendProfile.to_mapping()``). ``profile["driver"]`` is the scheme; an
    unknown scheme is a hard error listing the registered ones.
    """
    scheme = profile.get("driver")
    if not scheme:
        raise DriverError("connection profile has no 'driver' scheme")
    factory = _BY_SCHEME.get(str(scheme))
    if factory is None:
        raise DriverError(f"unknown driver scheme {scheme!r}; registered: {sorted(_BY_SCHEME)}")
    return factory(profile)


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
