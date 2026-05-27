"""VMHandle — runtime view of a brought-up VM, exposed to test code.

Phase 0 declares the shape. Phase 5 wires it through the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.communicators.base import Communicator


@dataclass
class VMHandle:
    """Test-code-facing view of a running VM."""

    name: str
    ip: str
    communicator: Communicator
