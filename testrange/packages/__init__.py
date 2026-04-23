"""Package manager definitions for VM provisioning.

One module per package manager: subclass :class:`AbstractPackage` in
its own file, re-export from here.  Top-level imports
(``from testrange.packages import Apt``) continue to work unchanged.
"""

from testrange.packages.apt import Apt
from testrange.packages.base import AbstractPackage
from testrange.packages.dnf import Dnf
from testrange.packages.homebrew import Homebrew
from testrange.packages.pip import Pip
from testrange.packages.winget import Winget

__all__ = [
    "AbstractPackage",
    "Apt",
    "Dnf",
    "Homebrew",
    "Pip",
    "Winget",
]
