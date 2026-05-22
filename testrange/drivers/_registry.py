"""Driver registry — maps Hypervisor data types and driver names to factories.

Concrete drivers register themselves at module import time. The orchestrator
and cleanup paths look up drivers by either:

- the user-facing Hypervisor data type from the Plan (``MockHypervisor``
  -> ``MockDriver``), or
- the driver class name recorded in state.json (``"MockDriver"``).

This is the only place where Hypervisor-type or driver-name dispatch lives;
no other module should know that, e.g., ``MockHypervisor`` maps to
``MockDriver``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError

_FROM_HYP: dict[type, Callable[[Any], HypervisorDriver]] = {}
_FROM_NAME: dict[str, Callable[[str], HypervisorDriver]] = {}


def register(
    *,
    hypervisor_cls: type,
    driver_name: str,
    from_hypervisor: Callable[[Any], HypervisorDriver],
    from_uri: Callable[[str], HypervisorDriver],
) -> None:
    """Register a driver's two construction paths.

    ``from_hypervisor`` builds the driver from the Plan-time Hypervisor data
    type (the orchestrator's entry point). ``from_uri`` builds the driver
    from a connection URI stored in state.json (the cleanup entry point).
    """
    _FROM_HYP[hypervisor_cls] = from_hypervisor
    _FROM_NAME[driver_name] = from_uri


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
