"""Winget (Windows Package Manager) packages."""

from __future__ import annotations

from testrange.packages.base import AbstractPackage


class Winget(AbstractPackage):
    """Install a package via Windows Package Manager (``winget``).

    :param name: The winget package ID (e.g. ``'Git.Git'``,
        ``'Microsoft.VisualStudioCode'``).

    Example::

        Winget("Git.Git")
        Winget("Notepad++.Notepad++")
    """

    @property
    def package_manager(self) -> str:
        """Return ``'winget'``.

        :returns: The string ``'winget'``.
        """
        return "winget"

    def native_package_name(self) -> None:
        """Return ``None`` — winget packages are installed via :meth:`install_commands`.

        :returns: ``None``.
        """
        return None

    def install_commands(self) -> list[str]:
        """Return the PowerShell command that installs this package.

        :returns: A one-element list containing the winget install command.
        """
        return [
            f"winget install --id {self.name} --silent --accept-package-agreements"
            " --accept-source-agreements"
        ]


__all__ = ["Winget"]
