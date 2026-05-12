"""VMHandle — runtime view of a brought-up VM, exposed to test code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.communicators.base import Communicator


@dataclass(frozen=True)
class VMHandle:
    """Test-code-facing view of a running VM.

    Transport-specific addressing (IPs, sockets, serial paths, guest-agent
    channels) lives on the bound ``communicator`` — different communicator
    types have different addressing needs. Read e.g. ``vm.communicator.host``
    when the communicator is an ``SSHCommunicator``.
    """

    name: str
    communicator: Communicator
