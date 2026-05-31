"""The generic, backend-agnostic ``Hypervisor`` Plan entry (CORE-7).

A concrete ``*Hypervisor`` (``MockHypervisor``, ``ProxmoxHypervisor``,
``LibvirtHypervisor``) conflates four jobs: portable topology, backend
selection (its type drives ``driver_for``), connection config, and
environment knobs. That forces a portable test to hard-code one backend.

This ``Hypervisor`` carries **only** job 1 — the portable topology
(networks/pools/vms, plus the optional transient ``build_switch``). It selects
no driver (deliberately *not* registered in the driver registry, which is what
marks it "generic / unpinned") and carries no connection. The backend is
supplied separately at run time via a connection profile (``--profile``);
``resolve_backend`` (CORE-10) folds this topology and that profile into the
binding the orchestrator consumes.

The ``build_switch`` is portable topology (ADR-0016): now that ``Switch.uplink``
is a profile-resolved logical name, the build switch carries nothing
host-specific, so it lives here alongside the run-phase networks rather than on
the binding (reversing the CORE-7/ADR-0014 placement, whose only rationale was
the old host-specific uplink).
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
    backend at run time with ``testrange run --profile <name>``. Without a
    profile a plan built on this type has no driver, so ``run``/``build`` error
    and point at ``--profile`` (CORE-10); ``describe`` still renders it.

    ``build_switch`` is the optional transient build-phase network (ADR-0016):
    ``None`` => an isolated no-egress build switch; a ``Switch`` is realized
    exactly like a run-phase one (a NAT egress build switch is
    ``Switch(uplink="<named>", sidecar=Sidecar(dhcp=True, dns=True, nat=True))``).
    """

    networks: Sequence[Switch] = field(default_factory=tuple)
    pools: Sequence[StoragePool] = field(default_factory=tuple)
    vms: Sequence[VMRecipe] = field(default_factory=tuple)
    build_switch: Switch | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "networks", tuple(self.networks))
        object.__setattr__(self, "pools", tuple(self.pools))
        object.__setattr__(self, "vms", tuple(self.vms))
        validate_hypervisor_plan(self.networks, self.pools, self.vms)

    @property
    def all_switches(self) -> tuple[Switch, ...]:
        return tuple(self.networks)


__all__ = ["Hypervisor"]
