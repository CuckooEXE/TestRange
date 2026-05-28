"""The generic, backend-agnostic ``Hypervisor`` Plan entry (CORE-7).

A concrete ``*Hypervisor`` (``MockHypervisor``, ``ProxmoxHypervisor``,
``LibvirtHypervisor``) conflates four jobs: portable topology, backend
selection (its type drives ``driver_for``), connection config, and
environment knobs. That forces a portable test to hard-code one backend.

This ``Hypervisor`` carries **only** job 1 — the portable topology
(networks/pools/vms). It selects no driver (deliberately *not* registered in
the driver registry, which is what marks it "generic / unpinned") and carries
no connection. The backend is supplied separately at run time via a connection
profile (``--connect``); ``resolve_backend`` (CORE-10) folds this topology and
that profile into the binding the orchestrator consumes.

Build egress (``build_switch`` / ``ManagedBuildSwitch``) is **not** here: it is
a backend-specific binding concern that rides on the resolved backend, not on
portable topology (CORE-7/CORE-10 decision, 2026-05-26).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from testrange.networks.validate import validate_hypervisor_plan

if TYPE_CHECKING:  # pragma: no cover
    from testrange.devices.pool.base import StoragePool
    from testrange.networks.base import Switch
    from testrange.vms.recipe import VMRecipe


@dataclass(frozen=True)
class Hypervisor:
    """A backend-agnostic Plan entry: portable topology only, no backend.

    Construct one from generic devices and pass it to :class:`Plan`; supply the
    backend at run time with ``testrange run --connect <profile>``. Without a
    profile a plan built on this type has no driver, so ``run``/``build`` error
    and point at ``--connect`` (CORE-10); ``describe`` still renders it.
    """

    networks: Sequence[Switch] = field(default_factory=tuple)
    pools: Sequence[StoragePool] = field(default_factory=tuple)
    vms: Sequence[VMRecipe] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "networks", tuple(self.networks))
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "vms", tuple(self.vms))
        validate_hypervisor_plan(self.networks, self.pools, self.vms)

    @property
    def all_switches(self) -> tuple[Switch, ...]:
        return tuple(self.networks)


__all__ = ["Hypervisor"]
