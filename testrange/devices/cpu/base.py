"""CPU device — count of vCPUs."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class CPU(Device):
    """Generic CPU spec. Exactly one CPU is allowed per VMSpec.

    ``nested`` is a portable "this guest must run hardware-accelerated VMs" knob
    (ADR-0021): a guest with ``nested=True`` needs the host's virtualization
    extensions (``vmx``/``svm``) exposed.
    """

    count: int
    nested: bool = False

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"CPU.count must be a positive int, got {self.count!r}")
