"""Package declarations baked into the install seed by Builders."""

from __future__ import annotations

from testrange.packages.apt import Apt
from testrange.packages.base import Package
from testrange.packages.pip import Pip

__all__ = ["Apt", "Package", "Pip"]
