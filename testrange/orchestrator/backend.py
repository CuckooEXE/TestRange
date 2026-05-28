"""Backend binding resolver (CORE-10).

Folds a Plan entry and an optional connection profile into a single
:class:`ResolvedBackend` the orchestrator consumes — the one place that decides
*which* driver runs, *how* it connects, and *what* build egress and teardown URI
it carries. This replaces the orchestrator reaching into ``plan.hypervisor`` for
the driver/env/uri directly, and it is where the portable-vs-pinned distinction
is enforced.

The pin/override matrix (``pinned = is_pinned(plan.hypervisor)``):

==================  ====================================================
(entry, profile)    resolution
==================  ====================================================
concrete + none     today's path: ``driver_for(hyp)``; build egress and
                    teardown URI from the concrete entry (full back-compat).
concrete + given    profile.scheme MUST equal the entry's scheme, else a
                    hard error; the driver is built from the profile
                    connection (``profile.build_driver()``); build egress
                    from the profile; topology still from the entry.
                    (Profile overrides *connection only* — a concrete entry
                    pins the driver.)
generic  + none     hard error: the plan is backend-agnostic; pass
                    ``--connect <profile>``.
generic  + given    driver from ``profile.build_driver()``; build egress
                    from the profile.
==================  ====================================================

Compatibility preflight is three layers; this module owns the first two:
(1) the static pin/driver-match above, raised here; (2) the portability lint
:func:`compatibility_findings` (a near-empty honest hook today). The third —
live capability findings against the resolved driver — runs inside the driver's
own ``preflight`` and is merged by the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from testrange.drivers import (
    driver_for,
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
    ``Switch | ManagedBuildSwitch | None``) — ``resolve_build_switch`` later
    folds it into the transient build Switch the build phase brings up.
    ``driver_uri`` is the teardown URI persisted into state.json so a later
    ``cleanup`` rebuilds the driver.
    """

    driver: HypervisorDriver
    build_switch: Switch | ManagedBuildSwitch | None
    driver_uri: str


def resolve_backend(plan: Plan, profile: BackendProfile | None) -> ResolvedBackend:
    """Resolve ``(plan entry, optional profile)`` into a :class:`ResolvedBackend`.

    Implements the pin/override matrix. Raises :class:`DriverError` on a pinned
    driver/profile mismatch or a backend-agnostic plan with no profile.
    """
    hyp = plan.hypervisor
    pinned = is_pinned(hyp)

    if pinned and profile is None:
        # A pinned entry is always a concrete *Hypervisor, which always carries a
        # build_switch — read it directly. driver_uri stays a getattr: the
        # in-memory MockHypervisor (the test backend) has no teardown URI, so it
        # omits the attribute and falls back to "".
        driver = driver_for(hyp)
        return ResolvedBackend(
            driver=driver,
            build_switch=hyp.build_switch,
            driver_uri=str(getattr(hyp, "driver_uri", "")),
        )

    if pinned and profile is not None:
        scheme = scheme_for_hypervisor(hyp)
        if profile.scheme != scheme:
            raise DriverError(
                f"connection profile selects driver {profile.scheme!r}, but the plan pins "
                f"a {type(hyp).__name__} ({scheme!r} backend); a concrete Hypervisor entry "
                f"pins the driver — a profile may override the connection only, not the driver. "
                f"Use the generic `Hypervisor` for a portable plan, or a {scheme!r} profile here."
            )
        driver = profile.build_driver()
        return ResolvedBackend(
            driver=driver,
            build_switch=profile.build_switch,
            driver_uri=_driver_uri(driver),
        )

    if not pinned and profile is None:
        raise DriverError(
            f"plan entry {type(hyp).__name__} is backend-agnostic and selects no driver; "
            f"pass --connect <profile> to bind it to a backend"
        )

    # generic + given
    assert profile is not None  # narrowed by the branches above (mypy)
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
