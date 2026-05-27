"""The top-level ``Plan`` declaration.

v0 enforces exactly one hypervisor (variadic call shape is in place for
the multi-hypervisor long-term TODO).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Plan:
    """The top-level declaration: one or more Hypervisor entries.

    v0 enforces exactly one hypervisor at runtime; the variadic call
    shape ``Plan(*hypervisors)`` is fixed so multi-hypervisor doesn't
    break the API.
    """

    hypervisors: tuple[Any, ...] = field(default_factory=tuple)

    def __init__(self, *hypervisors: Any) -> None:
        if len(hypervisors) == 0:
            raise ValueError("Plan() requires at least one hypervisor")
        if len(hypervisors) > 1:
            raise NotImplementedError(
                f"Plan() v0 supports exactly one hypervisor; got {len(hypervisors)}. "
                "Multi-hypervisor is a long-term TODO (TODO.md)."
            )
        object.__setattr__(self, "hypervisors", tuple(hypervisors))

    @property
    def hypervisor(self) -> Any:
        """The single hypervisor (v0 invariant)."""
        return self.hypervisors[0]
