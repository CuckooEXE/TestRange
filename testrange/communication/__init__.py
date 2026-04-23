"""Hypervisor-neutral VM communication backends.

:class:`SSHCommunicator` and :class:`WinRMCommunicator` are lazy-loaded
(PEP 562) so their third-party dependencies stay truly optional.

``guest-agent`` communicators are backend-specific — each hypervisor
backend ships its own implementation alongside the rest of its code,
because the transport (virtio-serial, REST, named pipes, …) is tied
to that backend's native control plane.
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
