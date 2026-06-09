"""NativeCommunicator — guest I/O through a hypervisor's native guest agent."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from testrange.communicators.base import Communicator, ExecResult
from testrange.exceptions import (
    CommunicatorAlreadyBoundError,
    CommunicatorError,
    GuestAgentError,
)

if TYPE_CHECKING:  # pragma: no cover
    from testrange.guest_io import GuestExec, GuestReadFile, GuestWriteFile


class NativeCommunicator(Communicator):
    """Communicator backed by a hypervisor's native in-guest agent.

    Backend-agnostic by design: it fronts whatever native agent the driver
    exposes — QEMU Guest Agent (libvirt, Proxmox), VMware Tools guest-ops,
    Hyper-V integration / PowerShell Direct. Constructed with no Plan-time
    arguments — the agent's identity is the VM itself. A thin shim over three
    VM-bound callables it is :meth:`bind`-ed with; it never sees a driver type
    or backend wire protocol.

    Bind-once: a bound communicator cannot be re-bound to another VM — construct
    a fresh instance per VM. ``close()`` is *not* terminal: like
    :class:`SSHCommunicator` it ends the current session and the next call
    re-establishes it, so a Plan can ``close()`` after power-cycling the guest
    and keep using the same communicator (the portable lifecycle/snapshot idiom).
    """

    def __init__(self) -> None:
        self._execute: GuestExec | None = None
        self._read_file: GuestReadFile | None = None
        self._write_file: GuestWriteFile | None = None
        # `_live` mirrors SSHCommunicator's `_client is not None`: cleared by
        # close() so the next call waits out the post-power-cycle window before
        # its real work (the agent needs a few seconds to answer after a reboot).
        self._live = False

    @property
    def is_bound(self) -> bool:
        return self._execute is not None

    def bind(
        self,
        *,
        execute: GuestExec,
        read_file: GuestReadFile,
        write_file: GuestWriteFile,
    ) -> None:
        """Bind to a live VM's guest agent. Called by the orchestrator."""
        if self._execute is not None:
            raise CommunicatorAlreadyBoundError(
                "NativeCommunicator already bound; construct a fresh instance per VM"
            )
        self._execute = execute
        self._read_file = read_file
        self._write_file = write_file
        # The orchestrator gates initial readiness (wait_communicators_ready), so
        # the freshly-bound session is treated as live; only a close() re-arms the
        # reconnect wait.
        self._live = True

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        self._check_bound()
        self._reconnect_if_needed()
        assert self._execute is not None  # narrowed by _check_bound
        return self._execute(argv, timeout=timeout, cwd=cwd)

    def read_file(self, path: str) -> bytes:
        self._check_bound()
        self._reconnect_if_needed()
        assert self._read_file is not None
        return self._read_file(path)

    def write_file(self, path: str, data: bytes) -> None:
        self._check_bound()
        self._reconnect_if_needed()
        assert self._write_file is not None
        self._write_file(path, data)

    def _check_bound(self) -> None:
        """Raise if the communicator was never bound."""
        if self._execute is None:
            raise CommunicatorError(
                "NativeCommunicator is not bound; the orchestrator must call .bind() first"
            )

    def _reconnect_if_needed(self) -> None:
        """Wait out the agent's post-reboot window after a close(), then mark live.

        The analogue of :meth:`SSHCommunicator._ensure_connected`: a Plan that
        power-cycles a guest closes the communicator to drop the stale session,
        and the guest agent takes a few seconds to answer again after the boot.
        Poll a trivial exec until it does (capped, with backoff) so the next real
        call doesn't race the reboot. A no-op once live.
        """
        if self._live:
            return
        assert self._execute is not None
        deadline = time.monotonic() + 120.0
        last: GuestAgentError | None = None
        while time.monotonic() < deadline:
            try:
                self._execute(("true",), timeout=10.0)
            except GuestAgentError as e:
                last = e  # agent not back up yet after the reboot
            else:
                self._live = True
                return
            time.sleep(2.0)
        raise CommunicatorError(
            f"native guest agent did not answer within 120s after reconnect: {last}"
        )

    def close(self) -> None:
        """End the current session. Not terminal and idempotent.

        The communicator stays bound; the next call re-establishes the session
        (mirrors :class:`SSHCommunicator`). The native agent is sessionless — the
        bound callables are VM-bound closures that survive a reboot — so there is
        nothing to tear down here; clearing ``_live`` just re-arms the reconnect
        wait for the next call.
        """
        self._live = False
