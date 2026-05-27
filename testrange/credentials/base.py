"""Credential ABC.

A credential is pure data: a username and (optionally) auth material.
Builders bake credentials into the disk; Communicators authenticate
against the running guest. Neither knows the other exists — the
orchestrator brokers.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class Credential(ABC):
    """Base class for all credentials. Subclasses add auth fields."""

    username: str
    admin: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.username, str) or not self.username:
            raise ValueError("Credential.username must be a non-empty string")
