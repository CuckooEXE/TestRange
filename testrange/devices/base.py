"""Device ABC — shared base for everything in a VMSpec's ``devices=[...]``."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class Device(ABC):  # noqa: B024  (marker class; abstract methods aren't required)
    """Abstract base for VM devices. Concretes carry data fields."""
