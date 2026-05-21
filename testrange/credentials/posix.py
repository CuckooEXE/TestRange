"""POSIX-style credentials (Linux/macOS/BSD guests)."""

from __future__ import annotations

from dataclasses import dataclass, field

from testrange.credentials.base import Credential
from testrange.utils import SSHKey


@dataclass(frozen=True)
class PosixCred(Credential):
    """POSIX user with optional password and/or SSH keypair.

    Auth precedence at use-time: SSH key if present, else password.
    Carrying both is legal — the credential is just data.

    Fields:
      username: POSIX username.
      password: optional plaintext password (rendered to a deterministic
        sha512 crypt by the builder when needed).
      ssh_key: optional SSH keypair. The public half is baked into
        authorized_keys; the private half is held in memory only and never
        written to the orchestrator host's filesystem.
      sudo: grant passwordless sudo (POSIX-specific). Translated by the
        builder into the right sudoers fragment.
      admin: cross-platform "elevated" flag inherited from Credential.
    """

    password: str | None = None
    ssh_key: SSHKey | None = None
    sudo: bool = False
    groups: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.password is None and self.ssh_key is None:
            raise ValueError(
                f"PosixCred({self.username!r}) needs at least one of password or ssh_key"
            )
