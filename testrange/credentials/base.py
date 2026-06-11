"""Credential ABC.

A credential is pure data: a username and (optionally) auth material.
Builders bake credentials into the disk; Communicators authenticate
against the running guest. Neither knows the other exists — the
orchestrator brokers.
"""

from __future__ import annotations

import re
from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class Credential(ABC):
    """Base class for all credentials. Subclasses add auth fields."""

    username: str
    admin: bool = False

    def __post_init__(self) -> None:
        if not self.username:
            raise ValueError("Credential.username must be a non-empty string")
        # The username is interpolated into guest-side shell (e.g. the nested
        # `usermod -aG ... <username>` provisioning command, CloudInit runcmd).
        # Constrain it to a POSIX-safe charset at this trust boundary so a
        # metacharacter username can't inject a command into that shell (CORE-98).
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*$", self.username):
            raise ValueError(
                "Credential.username must be POSIX-safe "
                rf"(match ^[a-zA-Z_][a-zA-Z0-9_-]*$); got {self.username!r}"
            )
