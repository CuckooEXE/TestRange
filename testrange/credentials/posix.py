"""POSIX-style credentials (Linux/macOS/BSD guests)."""

from __future__ import annotations

from dataclasses import dataclass, field

from testrange.credentials.base import Credential


@dataclass(frozen=True)
class PosixCred(Credential):
    """POSIX user with optional password and/or SSH public key.

    Auth precedence at use-time (PLAN.md decision 7): SSH pubkey if present,
    else password. Carrying both is legal — the credential is just data.

    Fields:
      username: POSIX username.
      password: optional plaintext password (rendered to a deterministic
        sha512 crypt by the builder when needed).
      pubkey: OpenSSH-format public key text. Baked into authorized_keys.
      privkey: OpenSSH-format private key text. Held in memory only; never
        written to the orchestrator host's filesystem.
      sudo: grant passwordless sudo (POSIX-specific). Translated by the
        builder into the right sudoers fragment.
      admin: cross-platform "elevated" flag inherited from Credential.
    """

    password: str | None = None
    pubkey: str | None = None
    privkey: str | None = None
    sudo: bool = False
    extra_groups: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.password is None and self.pubkey is None:
            raise ValueError(
                f"PosixCred({self.username!r}) needs at least one of password or pubkey"
            )
        if self.privkey is not None and self.pubkey is None:
            raise ValueError(
                f"PosixCred({self.username!r}) has privkey without pubkey; provide both or neither"
            )
