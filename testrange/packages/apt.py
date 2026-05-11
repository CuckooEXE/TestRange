"""Apt — Debian/Ubuntu apt-get install at builder time."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.packages.base import Package


@dataclass(frozen=True)
class Apt(Package):
    """A package installed via apt during the install phase."""
