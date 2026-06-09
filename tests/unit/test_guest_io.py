"""guest_io: the shared callable Protocols match the Communicator surface.

The typed local assignments below are the real check — mypy --strict fails the
gate if a Communicator method shape drifts away from its Protocol. (An
``isinstance`` against these Protocols would prove nothing: they are
``@runtime_checkable`` with ``__call__`` as their only member, so the check is
``True`` for any callable regardless of signature — so we don't write it.)
"""

from __future__ import annotations

from testrange.communicators import SSHCommunicator
from testrange.guest_io import ExecResult, GuestExec, GuestReadFile, GuestWriteFile


class TestProtocolShapes:
    def test_communicator_methods_satisfy_guest_io_protocols(self) -> None:
        ssh = SSHCommunicator("alice")
        # Assignment-with-annotation is the assertion: mypy --strict rejects a
        # method whose signature no longer matches the Protocol.
        _execute: GuestExec = ssh.execute
        _read_file: GuestReadFile = ssh.read_file
        _write_file: GuestWriteFile = ssh.write_file
        assert (_execute, _read_file, _write_file) == (
            ssh.execute,
            ssh.read_file,
            ssh.write_file,
        )


class TestExecResultReexport:
    def test_exec_result_is_the_canonical_type(self) -> None:
        from testrange.communicators.base import ExecResult as Canonical

        assert ExecResult is Canonical
