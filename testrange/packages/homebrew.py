"""Homebrew packages (macOS / Linuxbrew)."""

from __future__ import annotations

from testrange.packages.base import AbstractPackage

_BREW_INSTALL_CMD = (
    'NONINTERACTIVE=1 su -s /bin/bash -c'
    ' \'$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\''
    ' {user}'
)
"""Shell command template that installs Homebrew non-interactively.

The ``{user}`` placeholder is substituted with the non-root username at
provisioning time.
"""

_BREW_PKG_CMD = "su -s /bin/bash -c 'brew install {pkg}' {user}"
"""Shell command template for installing a single Homebrew formula.

Placeholders: ``{pkg}`` — formula name; ``{user}`` — non-root username.
"""


class Homebrew(AbstractPackage):
    """Install a package via Homebrew.

    .. note::
        Homebrew refuses to run as ``root``.  At least one non-root user must
        be present in the VM's credential list.

    :param name: The Homebrew formula or cask name (e.g. ``'hello'``, ``'wget'``).

    Example::

        Homebrew("hello")
        Homebrew("gh")
    """

    @property
    def package_manager(self) -> str:
        """Return ``'brew'``.

        :returns: The string ``'brew'``.
        """
        return "brew"

    def native_package_name(self) -> None:
        """Return ``None`` — Homebrew packages are installed via :meth:`install_commands`.

        :returns: ``None``.
        """
        return None

    def install_commands(self) -> list[str]:
        """Return the shell command template that installs this formula.

        The ``{brew_user}`` placeholder must be substituted by the caller before
        execution.

        :returns: List of shell command templates.
        """
        return [_BREW_PKG_CMD.format(pkg=self.name, user="{brew_user}")]

    @staticmethod
    def install_homebrew_command() -> str:
        """Return the shell command template that installs Homebrew itself.

        :returns: Shell command template with a ``{user}`` placeholder.
        """
        return _BREW_INSTALL_CMD


__all__ = ["Homebrew"]
