"""CPU device — count of vCPUs."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.devices.base import Device


@dataclass(frozen=True)
class CPU(Device):
    """Generic CPU spec. Exactly one CPU is allowed per VMSpec.

    ``nested`` is a portable "this guest must run hardware-accelerated VMs" knob
    (ADR-0021): a guest with ``nested=True`` needs the host's virtualization
    extensions (``vmx``/``svm``) exposed. The libvirt backend already passes the
    host CPU through (``<cpu mode='host-passthrough'/>``), so the flag exists to
    give preflight a hook — the L0 driver verifies host nested KVM is enabled and
    fails loud early rather than letting an inner VM fail to boot under emulation.
    """

    count: int
    nested: bool = False

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"CPU.count must be a positive int, got {self.count!r}")
