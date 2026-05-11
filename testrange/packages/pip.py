"""Pip — Python package installed during the install phase."""

from __future__ import annotations

from dataclasses import dataclass

from testrange.packages.base import Package


@dataclass(frozen=True)
class Pip(Package):
    """A Python package installed via pip during the install phase."""
