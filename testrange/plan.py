"""The top-level ``Plan`` declaration.

A Plan currently wraps exactly one Hypervisor. The constructor shape
``Plan(name, *hypervisors)`` is fixed so future multi-hypervisor support
doesn't change the public call shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Plan:
    """The top-level declaration: a name plus one or more Hypervisor entries."""

    # No field defaults: the hand-written __init__ below owns construction
    # (it requires a non-empty name and at least one hypervisor). These
    # declarations exist only to drive the frozen dataclass's __eq__/__repr__.
    name: str
    hypervisors: tuple[Any, ...]

    def __init__(self, name: str, *hypervisors: Any) -> None:
        # The plan name namespaces every derived resource: stable MACs
        # (compose_mac), backend resource names, and build cache keys. An
        # unnamed plan would silently share that namespace with any other
        # unnamed plan, so it leads as a required positional rather than
        # defaulting.
        if not name:
            raise ValueError(
                "Plan(name, ...) requires a non-empty name; it namespaces stable "
                "MACs, backend resource names, and the build cache"
            )
        if len(hypervisors) == 0:
            raise ValueError("Plan(name, ...) requires at least one hypervisor")
        if len(hypervisors) > 1:
            raise NotImplementedError(
                f"Plan() currently supports exactly one hypervisor; got {len(hypervisors)}."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "hypervisors", tuple(hypervisors))

    @property
    def hypervisor(self) -> Any:
        """The single hypervisor."""
        return self.hypervisors[0]
