"""RunningVM — runtime view of a brought-up VM, exposed to test code.

Distinct from :class:`~testrange.handles.VMHandle`, the plan-construction
handle ``hyp.add_vm`` returns: a ``RunningVM`` exists only inside a live run,
carries the bound communicator, and is reached via ``orch.vms["name"]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from testrange.communicators.base import Communicator


@dataclass(frozen=True)
class RunningVM:
    """Test-code-facing view of a running VM.

    - ``name`` is the user-facing Plan name (e.g., ``"web"``).
    - ``backend_name`` is the driver-side handle (e.g., ``"tr_vm_abc_web"``)
      — pass it to ``orch.driver.create_snapshot(...)`` / ``destroy_vm(...)``
      / etc. for host-side VM control.
    - ``communicator`` is for guest-side I/O. Transport-specific addressing
      (IPs, sockets, serial paths, guest-agent channels) lives on it —
      different communicator types have different addressing needs. Read
      e.g. ``vm.communicator.host`` when the communicator is an
      ``SSHCommunicator``.
    """

    name: str
    backend_name: str
    communicator: Communicator
