"""Package ABC."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class Package(ABC):
    """Abstract package. Concretes carry the package name and any extras."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__}.name must be a non-empty string")
