"""ESXI-5: VMware Tools guest-ops credential gate (``_auth``).

VMware Tools authenticates against the guest OS on every call, so each guest-op
binds a per-call username+password credential (CORE-60, ADR-0008). The two
rejection paths are pure logic (the client/SOAP plane is never touched), so they
need no live ESXi — mirroring test_libvirt_guest / test_proxmox_guest.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from testrange.credentials.posix import PosixCred
from testrange.drivers.esxi import _guest
from testrange.exceptions import GuestAgentError
from testrange.utils import SSHKey


class _RecordingLock:
    """A call_lock stand-in that exposes its current acquisition depth."""

    def __init__(self) -> None:
        self.depth = 0

    def __enter__(self) -> _RecordingLock:
        self.depth += 1
        return self

    def __exit__(self, *_exc: object) -> None:
        self.depth -= 1


def test_guest_op_serializes_soap_call_but_not_transfer() -> None:
    # ESXI-32: each guest-ops SOAP call must run under client.call_lock (the
    # parallel I/O phases share one pyVmomi stub), but the byte transfer must run
    # OUTSIDE the lock so concurrent guests still overlap.
    lock = _RecordingLock()
    seen: dict[str, int] = {}

    class _FM:
        def InitiateFileTransferFromGuest(self, *, vm: Any, auth: Any, guestFilePath: str) -> Any:
            seen["soap"] = lock.depth
            return SimpleNamespace(url="http://host/x")

    def _guest_file_get(_url: str) -> bytes:
        seen["transfer"] = lock.depth
        return b"DATA"

    client = SimpleNamespace(
        call_lock=lock,
        vim=MagicMock(),
        content=SimpleNamespace(
            guestOperationsManager=SimpleNamespace(processManager=MagicMock(), fileManager=_FM())
        ),
        require_vm=lambda _name: SimpleNamespace(),
        guest_file_get=_guest_file_get,
    )
    read = _guest.make_read_file(cast(Any, client), "tr-vm-x", PosixCred("u", password="p"))
    assert read("/etc/hostname") == b"DATA"
    assert seen["soap"] == 1  # SOAP call held the call_lock
    assert seen["transfer"] == 0  # transfer ran with the lock released


def _fake_client(pm: Any, fm: Any, guest_file_get: Any = None) -> Any:
    return SimpleNamespace(
        call_lock=_RecordingLock(),
        vim=MagicMock(),
        content=SimpleNamespace(
            guestOperationsManager=SimpleNamespace(processManager=pm, fileManager=fm)
        ),
        require_vm=lambda _name: SimpleNamespace(),
        guest_file_get=guest_file_get,
    )


class _ExitedPM:
    """Process manager whose program starts and immediately reports exit 0."""

    def StartProgramInGuest(self, *, vm: Any, auth: Any, spec: Any) -> int:
        return 1234

    def ListProcessesInGuest(self, *, vm: Any, auth: Any, pids: Any) -> Any:
        return [SimpleNamespace(exitCode=0)]


def test_exec_poll_fault_is_translated_to_guest_agent_error() -> None:
    # ESXI-35: NativeCommunicator's reconnect loop retries only GuestAgentError
    # (communicators/native.py), so a raw pyVmomi fault from the
    # ListProcessesInGuest poll must surface as GuestAgentError, not escape raw.
    class _PM(_ExitedPM):
        def ListProcessesInGuest(self, *, vm: Any, auth: Any, pids: Any) -> Any:
            raise RuntimeError("vix fault: VMware Tools is not running")

    client = _fake_client(_PM(), MagicMock())
    execute = _guest.make_execute(cast(Any, client), "tr-vm-x", PosixCred("u", password="p"))
    with pytest.raises(GuestAgentError, match="exec poll failed on 'tr-vm-x'"):
        execute(("true",))


def test_exec_output_transfer_fault_is_translated_to_guest_agent_error() -> None:
    # ESXI-35: same contract for the exec-output temp-file reads — a raw fault
    # from InitiateFileTransferFromGuest must surface as GuestAgentError.
    class _FM:
        def InitiateFileTransferFromGuest(self, *, vm: Any, auth: Any, guestFilePath: str) -> Any:
            raise RuntimeError("vim.fault.GuestOperationsUnavailable")

    client = _fake_client(_ExitedPM(), _FM())
    execute = _guest.make_execute(cast(Any, client), "tr-vm-x", PosixCred("u", password="p"))
    with pytest.raises(GuestAgentError, match="failed on 'tr-vm-x'"):
        execute(("true",))


def test_exec_output_http_get_fault_is_translated_to_guest_agent_error() -> None:
    # ESXI-35: the HTTPS byte transfer is the second leg of _read; a raw
    # requests-level failure must also surface as GuestAgentError.
    class _FM:
        def InitiateFileTransferFromGuest(self, *, vm: Any, auth: Any, guestFilePath: str) -> Any:
            return SimpleNamespace(url="http://host/x")

    def _guest_file_get(_url: str) -> bytes:
        raise RuntimeError("connection reset by peer")

    client = _fake_client(_ExitedPM(), _FM(), _guest_file_get)
    execute = _guest.make_execute(cast(Any, client), "tr-vm-x", PosixCred("u", password="p"))
    with pytest.raises(GuestAgentError, match="failed on 'tr-vm-x'"):
        execute(("true",))


def test_auth_requires_a_credential() -> None:
    with pytest.raises(GuestAgentError, match="require a guest credential"):
        _guest._auth(cast(Any, None), None, "tr-vm-x")


def test_auth_rejects_key_only_credential() -> None:
    # A key-only PosixCred has no password; VMware Tools can't use an SSH key,
    # so guest-ops must reject it loudly rather than build a passwordless auth.
    key_only = PosixCred("admin", ssh_key=SSHKey.generate(comment="t"))
    with pytest.raises(GuestAgentError, match="key-only credentials are not usable"):
        _guest._auth(cast(Any, None), key_only, "tr-vm-x")
