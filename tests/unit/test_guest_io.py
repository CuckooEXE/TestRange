"""guest_io: the shared callable Protocols match the Communicator surface.

The typed local assignments are what pin the signatures — mypy --strict
fails the gate if a method shape drifts away from its Protocol. The
isinstance checks are a runtime smoke that the methods are callable.
"""

from __future__ import annotations

from testrange.communicators import SSHCommunicator
from testrange.guest_io import ExecResult, GuestExec, GuestReadFile, GuestWriteFile


class TestProtocolShapes:
    def test_execute_matches_guest_exec(self) -> None:
        ssh = SSHCommunicator("alice")
        fn: GuestExec = ssh.execute
        assert isinstance(fn, GuestExec)

    def test_read_file_matches_guest_read_file(self) -> None:
        ssh = SSHCommunicator("alice")
        fn: GuestReadFile = ssh.read_file
        assert isinstance(fn, GuestReadFile)

    def test_write_file_matches_guest_write_file(self) -> None:
        ssh = SSHCommunicator("alice")
        fn: GuestWriteFile = ssh.write_file
        assert isinstance(fn, GuestWriteFile)


class TestExecResultReexport:
    def test_exec_result_is_the_canonical_type(self) -> None:
        from testrange.communicators.base import ExecResult as Canonical

        assert ExecResult is Canonical
