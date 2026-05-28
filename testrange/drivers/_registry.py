"""Driver registry — driver-class-name dispatch + Hypervisor-type scheme markers.

Concrete drivers register themselves at module import time. Two paths share the
registry now (CORE-19 collapsed the third):

- by **driver class name** recorded in state.json (``"MockDriver"``) — the
  cleanup entry point (``driver_for_name``);
- by **Hypervisor data type** for pin introspection (``scheme_for_hypervisor``,
  ``is_pinned``) — the binding resolver (CORE-10) uses this to detect a
  topology-only scheme marker (CORE-19) and to enforce that ``--connect``'s
  scheme matches.

The Plan-entry-type-to-driver path (``driver_for(hyp)``) is gone with CORE-19:
a concrete ``*Hypervisor`` is now a topology-only scheme marker carrying no
connection, so the driver is *always* built from the ``--connect`` profile
(``BackendProfile.build_driver``) or, on cleanup, rebuilt from the persisted
teardown URI via ``driver_for_name``.

The ``--connect`` profile dispatch lives separately in
:mod:`testrange.connect` (``_PROFILE_BY_SCHEME``); the ``scheme`` recorded here
is what the binding resolver compares the profile's scheme against.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError

_FROM_NAME: dict[str, Callable[[str], HypervisorDriver]] = {}
_SCHEME_FOR_HYP: dict[type, str] = {}


def register(
    *,
    hypervisor_cls: type,
    driver_name: str,
    scheme: str,
    from_uri: Callable[[str], HypervisorDriver],
) -> None:
    """Register a driver's cleanup factory and its Hypervisor-type scheme marker.

    ``from_uri`` rebuilds the driver from the connection URI stored in
    state.json (the cleanup entry point). ``hypervisor_cls`` is the concrete
    topology-only ``*Hypervisor`` subclass that scheme-pins to this backend;
    ``scheme`` is the short token (``"mock"``, ``"proxmox"``, ``"libvirt"``)
    the binding resolver matches a ``--connect`` profile against.
    """
    _FROM_NAME[driver_name] = from_uri
    _SCHEME_FOR_HYP[hypervisor_cls] = scheme


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
    :class:`~testrange.hypervisor.Hypervisor` — so it pins no scheme and the
    binding resolver accepts any registered ``--connect`` profile.
    """
    return _SCHEME_FOR_HYP.get(type(hypervisor))


def is_pinned(hypervisor: Any) -> bool:
    """True if this Plan entry is a scheme marker (its type is registered)."""
    return type(hypervisor) in _SCHEME_FOR_HYP
