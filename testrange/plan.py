"""The top-level ``Plan`` declaration.

A Plan currently wraps exactly one Hypervisor. The variadic constructor
shape ``Plan(*hypervisors)`` is fixed so future multi-hypervisor support
doesn't change the public call shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Plan:
    """The top-level declaration: one or more Hypervisor entries."""

    # No field defaults: the hand-written __init__ below owns construction
    # (it requires at least one hypervisor and a non-empty name). These
    # declarations exist only to drive the frozen dataclass's __eq__/__repr__.
    hypervisors: tuple[Any, ...]
    name: str

    def __init__(self, *hypervisors: Any, name: str = "") -> None:
        if len(hypervisors) == 0:
            raise ValueError("Plan() requires at least one hypervisor")
        if len(hypervisors) > 1:
            raise NotImplementedError(
                f"Plan() currently supports exactly one hypervisor; got {len(hypervisors)}."
            )
        # The plan name namespaces every derived resource: stable MACs
        # (compose_mac), backend resource names, and build cache keys.
        # An unnamed plan would silently share that namespace with any other
        # unnamed plan, so require it rather than defaulting.
        if not name:
            raise ValueError(
                "Plan(name=...) is required; it namespaces stable MACs, "
                "backend resource names, and the build cache"
            )
        object.__setattr__(self, "hypervisors", tuple(hypervisors))
        object.__setattr__(self, "name", name)

    @property
    def hypervisor(self) -> Any:
        """The single hypervisor."""
        return self.hypervisors[0]
