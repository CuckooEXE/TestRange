"""Backend binding resolver (CORE-10 / CORE-19).

Folds a Plan entry and the required connection profile into a single
:class:`ResolvedBackend` the orchestrator consumes — the one place that decides
*which* driver runs, *how* it connects (incl. the named-uplink map and the
teardown URI it carries). The build switch is portable topology on the plan now
(ADR-0016), not a binding concern.

Under CORE-19 the matrix has only two cells that actually bind, because a
concrete ``*Hypervisor`` is a topology-only scheme marker now (it carries no
connection):

==================  ====================================================
(entry, profile)    resolution
==================  ====================================================
concrete + given    profile.scheme MUST equal the entry's scheme, else a
                    hard error; driver = ``profile.build_driver()``;
                    teardown URI from the driver.
concrete + none     hard error: pass ``--profile <name>`` (a concrete
                    entry only constrains *which* scheme is allowed).
generic  + given    driver = ``profile.build_driver()``.
generic  + none     hard error: backend-agnostic plan; pass ``--profile``.
==================  ====================================================

Compatibility preflight is three layers; this module owns the first two:
(1) the scheme-pin/profile-match above, raised here; (2) the portability lint
:func:`compatibility_findings` (a near-empty honest hook today). The third —
live capability findings against the resolved driver — runs inside the driver's
own ``preflight`` and is merged by the orchestrator.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from testrange.drivers import (
    is_pinned,
    scheme_for_hypervisor,
)
from testrange.drivers.base import HypervisorDriver
from testrange.exceptions import DriverError
from testrange.preflight import PreflightFinding

if TYPE_CHECKING:  # pragma: no cover
    from testrange.connect import BackendProfile
    from testrange.devices.network import StaticAddr
    from testrange.plan import Plan


@dataclass(frozen=True)
class ResolvedBackend:
    """The single backend binding the orchestrator runs against.

    The build switch is **not** here — it is portable topology on the plan's
    ``Hypervisor`` now (ADR-0016), so the orchestrator reads it from
    ``plan.hypervisor.build_switch`` directly. ``driver_uri`` is the teardown URI
    persisted into state.json so a later ``cleanup`` rebuilds the driver via
    :func:`~testrange.drivers.driver_for_name`.
    """

    driver: HypervisorDriver
    driver_uri: str
    # Per-uplink static sidecar addressing from the profile (NET-8), keyed by
    # logical uplink name. The orchestrator injects it into a Switch's sidecar so
    # a host-NAT'd uplink that won't DHCP the sidecar still egresses. Empty for an
    # in-plan binding or a profile that declares no table-form uplinks.
    uplink_addrs: Mapping[str, StaticAddr] = field(default_factory=dict)


def resolve_backend(plan: Plan, profile: BackendProfile | None) -> ResolvedBackend:
    """Resolve ``(plan entry, optional profile)`` into a :class:`ResolvedBackend`.

    Implements the CORE-19 matrix. Raises :class:`DriverError` on a missing
    profile (concrete or generic) or a concrete/profile scheme mismatch.
    """
    hyp = plan.hypervisor
    pinned = is_pinned(hyp)

    if profile is None:
        if pinned:
            scheme = scheme_for_hypervisor(hyp)
            raise DriverError(
                f"plan entry {type(hyp).__name__} pins the {scheme!r} backend but no "
                f"connection profile was supplied; pass --profile <name> (the entry is "
                f"a topology-only scheme marker — it carries no connection)"
            )
        raise DriverError(
            f"plan entry {type(hyp).__name__} is backend-agnostic and selects no driver; "
            f"pass --profile <name> to bind it to a backend"
        )

    if pinned:
        scheme = scheme_for_hypervisor(hyp)
        if profile.scheme != scheme:
            raise DriverError(
                f"connection profile selects driver {profile.scheme!r}, but the plan pins "
                f"a {type(hyp).__name__} ({scheme!r} backend); a concrete Hypervisor entry "
                f"constrains which scheme is allowed. Use the generic `Hypervisor` for a "
                f"portable plan, or a {scheme!r} profile here."
            )

    driver = profile.build_driver()
    return ResolvedBackend(
        driver=driver,
        driver_uri=_driver_uri(driver),
        uplink_addrs=profile.uplink_addrs,
    )


def compatibility_findings(plan: Plan, driver: HypervisorDriver) -> tuple[PreflightFinding, ...]:
    """Portability lint: is this (already backend-agnostic) plan realizable on ``driver``?

    Layer 2 of the compatibility preflight. The topology layer is 100%
    backend-agnostic today (no backend-specific device/builder/network/vm
    subclasses exist), so there is nothing to reject and this returns ``()``.
    It is the honest hook for the day a backend-specific device subclass lands:
    that subclass would declare which drivers realize it, and this function
    would emit a blocking finding when the resolved driver isn't among them.
    The orchestrator merges the result into the driver's own preflight report.
    """
    del plan, driver
    return ()


def _driver_uri(driver: HypervisorDriver) -> str:
    """The driver's teardown URI (every concrete driver exposes ``.uri``)."""
    return str(getattr(driver, "uri", ""))


__all__ = ["ResolvedBackend", "compatibility_findings", "resolve_backend"]
