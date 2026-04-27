"""Backend-agnostic switch spec.

:class:`Switch` is a sibling of every backend-specific switch
(``<Backend>Switch`` under :mod:`testrange.backends.<backend>`) on
:class:`~testrange.networks.base.AbstractSwitch` — same architecture
as the :class:`~testrange.vms.generic.GenericVM` /
``<Backend>VM`` split.  Use it for orchestrator declarations that
don't care which backend ends up materialising the switch; the
orchestrator translates each ``Switch`` into its own native type at
``__enter__`` time.

Cannot itself :meth:`start` or :meth:`stop` — it's a pure spec
container.  Calling those raises immediately with a clear message: a
``Switch`` should never reach the provisioning code paths because
the orchestrator has converted it first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.exceptions import NetworkError
from testrange.networks.base import AbstractSwitch

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator


class Switch(AbstractSwitch):
    """Backend-agnostic switch specification.

    Field-for-field translatable to any backend's concrete
    ``<Backend>Switch``.  Pass it as
    ``Orchestrator(switches=[Switch(...)])`` when the test doesn't
    care which backend hosts it; the orchestrator will swap each
    ``Switch`` for its native concrete switch class before any
    backend code touches it.

    Example::

        sw = Switch("CorpNet", switch_type="vlan", uplinks=["eno1"])
        net = VirtualNetwork(
            "Mgmt", "10.0.10.0/24", internet=True, switch=sw,
        )
        Orchestrator(switches=[sw], networks=[net], vms=[...])

    See :class:`AbstractSwitch` for the field semantics.
    """

    def __init__(
        self,
        name: str,
        switch_type: str | None = None,
        uplinks: Sequence[str] | None = None,
    ) -> None:
        super().__init__(name=name, switch_type=switch_type, uplinks=uplinks)

    def _generic_switch_misuse(self) -> NetworkError:
        return NetworkError(
            f"Switch {self.name!r}: backend operation called on a "
            "spec-only Switch.  This means the orchestrator failed to "
            "convert it to its backend-specific switch type at "
            "__enter__; either call the orchestrator inside a "
            "``with`` block or pass a backend-specific switch directly."
        )

    def start(self, context: AbstractOrchestrator) -> None:  # noqa: D401
        del context
        raise self._generic_switch_misuse()

    def stop(self, context: AbstractOrchestrator) -> None:  # noqa: D401
        del context
        raise self._generic_switch_misuse()

    def backend_name(self) -> str:
        # Generic specs don't have a backend name; this is only
        # reached if the orchestrator forgot to promote.
        raise self._generic_switch_misuse()


__all__ = ["Switch"]
