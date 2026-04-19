"""Hypervisor-neutral VM communication backends.

:class:`SSHCommunicator` and :class:`WinRMCommunicator` are lazy-loaded
(PEP 562) so their third-party dependencies stay truly optional.

The libvirt / QEMU guest agent communicator lives with its backend
at :class:`testrange.backends.libvirt.GuestAgentCommunicator` — it
speaks QEMU's JSON-RPC protocol through a libvirt-managed
virtio-serial channel, which ties it to that backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from testrange.communication.base import AbstractCommunicator, ExecResult

if TYPE_CHECKING:
    from testrange.communication.ssh import SSHCommunicator
    from testrange.communication.winrm import WinRMCommunicator


__all__ = [
    "AbstractCommunicator",
    "ExecResult",
    "SSHCommunicator",
    "WinRMCommunicator",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve optional backends on first access (PEP 562)."""
    if name == "SSHCommunicator":
        from testrange.communication.ssh import SSHCommunicator
        return SSHCommunicator
    if name == "WinRMCommunicator":
        from testrange.communication.winrm import WinRMCommunicator
        return WinRMCommunicator
    raise AttributeError(
        f"module 'testrange.communication' has no attribute {name!r}"
    )
