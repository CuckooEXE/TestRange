"""Backend binding resolver (CORE-10 / CORE-19).

Folds a Plan entry and the required connection profile into a single
:class:`ResolvedBackend` the orchestrator consumes — the one place that decides
*which* driver runs, *how* it connects, and *what* build egress and teardown URI
it carries.

Under CORE-19 the matrix has only two cells that actually bind, because a
concrete ``*Hypervisor`` is a topology-only scheme marker now (it carries no
connection):

==================  ====================================================
(entry, profile)    resolution
==================  ====================================================
concrete + given    profile.scheme MUST equal the entry's scheme, else a
                    hard error; driver = ``profile.build_driver()``;
                    build egress + teardown URI from the profile/driver.
concrete + none     hard error: pass ``--connect <profile>`` (a concrete
                    entry only constrains *which* scheme is allowed).
generic  + given    driver = ``profile.build_driver()``; build egress
                    from the profile.
generic  + none     hard error: backend-agnostic plan; pass ``--connect``.
==================  ====================================================

Compatibility preflight is three layers; this module owns the first two:
(1) the scheme-pin/profile-match above, raised here; (2) the portability lint
:func:`compatibility_findings` (a near-empty honest hook today). The third —
live capability findings against the resolved driver — runs inside the driver's
own ``preflight`` and is merged by the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    from testrange.networks.base import ManagedBuildSwitch, Switch
    from testrange.plan import Plan


@dataclass(frozen=True)
class ResolvedBackend:
    """The single backend binding the orchestrator runs against.

    ``build_switch`` is the user-*declared* build switch (the binding's env-knob,
    a :class:`~testrange.networks.base.ManagedBuildSwitch` or ``None``) —
    ``resolve_build_switch`` later folds it into the transient build Switch the
    build phase brings up. ``driver_uri`` is the teardown URI persisted into
    state.json so a later ``cleanup`` rebuilds the driver via
    :func:`~testrange.drivers.driver_for_name`.
    """

    driver: HypervisorDriver
    build_switch: Switch | ManagedBuildSwitch | None
    driver_uri: str


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
                f"connection profile was supplied; pass --connect <profile> (the entry is "
                f"a topology-only scheme marker — it carries no connection)"
            )
        raise DriverError(
            f"plan entry {type(hyp).__name__} is backend-agnostic and selects no driver; "
            f"pass --connect <profile> to bind it to a backend"
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
        build_switch=profile.build_switch,
        driver_uri=_driver_uri(driver),
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
