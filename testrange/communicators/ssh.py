"""SSHCommunicator — paramiko-backed SSH transport.

Phase 0 ships the Plan-time skeleton:
  - ``__init__`` takes a username string.
  - ``bind(host=..., credential=...)`` is declared but raises
    ``NotImplementedError`` until Phase 5.

The single-use guard exists from day one so misuse fails loud even before
the runtime is implemented.
"""

from __future__ import annotations

from collections.abc import Sequence

from testrange.communicators.base import Communicator, ExecResult
from testrange.credentials.posix import PosixCred
from testrange.exceptions import CommunicatorAlreadyBoundError


class SSHCommunicator(Communicator):
    """SSH transport; binds at run-phase bring-up.

    Plan-time::

        communicator=SSHCommunicator("myuser")

    The username is resolved against the VMRecipe's builder.credentials by
    the orchestrator at bind time.
    """

    def __init__(self, username: str) -> None:
        if not isinstance(username, str) or not username:
            raise ValueError("SSHCommunicator(username) must be a non-empty string")
        self._username = username
        self._bound = False
        self._host: str | None = None
        self._credential: PosixCred | None = None

    @property
    def username(self) -> str:
        """The username this communicator wants to authenticate as."""
        return self._username

    @property
    def is_bound(self) -> bool:
        return self._bound

    def bind(self, *, host: str, credential: PosixCred) -> None:
        """Bind to a live VM. Called by the orchestrator at run-phase bring-up."""
        if self._bound:
            raise CommunicatorAlreadyBoundError(
                f"SSHCommunicator({self._username!r}) already bound; construct a fresh instance per VM"
            )
        if not isinstance(host, str) or not host:
            raise ValueError("SSHCommunicator.bind(host=...) must be a non-empty string")
        if not isinstance(credential, PosixCred):
            raise TypeError(
                f"SSHCommunicator.bind(credential=...) must be a PosixCred, got {type(credential).__name__}"
            )
        if credential.username != self._username:
            raise ValueError(
                f"credential.username={credential.username!r} does not match "
                f"SSHCommunicator username={self._username!r}"
            )
        self._host = host
        self._credential = credential
        self._bound = True

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        cwd: str | None = None,
    ) -> ExecResult:
        raise NotImplementedError("SSHCommunicator.execute lands in Phase 5")

    def read_file(self, path: str) -> bytes:
        raise NotImplementedError("SSHCommunicator.read_file lands in Phase 5")

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("SSHCommunicator.write_file lands in Phase 5")

    def close(self) -> None:
        """No-op until Phase 5."""
